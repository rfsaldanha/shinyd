#!/usr/bin/env python3
"""Run two trusted Shiny fixtures with different R versions through a loopback proxy."""

import json
import os
import secrets
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
IMAGES = json.loads((ROOT / "experiments/r-images.json").read_text())["images"]


def command(*args, timeout=60):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{args[0]}: {result.stderr[:2000]}")
    return result.stdout.strip()


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main():
    proxy_path = ROOT / "dist/shinyd-proxy-probe"
    if not proxy_path.is_file():
        raise RuntimeError("Compile dist/shinyd-proxy-probe antes de executar.")
    if os.geteuid() == 0:
        raise RuntimeError("Este experimento deve rodar sem sudo.")
    report = {
        "schema_version": 1,
        "observed_at": datetime.now(UTC).isoformat(),
        "scope": "Shiny + reverse proxy experimental, somente loopback",
        "production_ready": False,
        "tests": [],
        "images": IMAGES,
        "limitations": [
            "Não homologa login central, criação de imagens com renv ou isolamento AppArmor.",
            "Containers rootless de fixtures confiáveis; rede de produção ainda não homologada.",
        ],
    }
    names = []
    proxy = None
    prefix = "shiny-session-" + uuid.uuid4().hex[:10]
    try:
        port = free_port()
        routes = []
        for index, image in enumerate(IMAGES):
            label = chr(ord("a") + index)
            name = prefix + "-" + label
            names.append(name)
            # Pull by digest, never silently resolve a mutable tag during a test.
            command("podman", "image", "exists", image["image"])
            command(
                "podman",
                "run",
                "--detach",
                "--name",
                name,
                "--label",
                "org.shinyd.test=" + prefix,
                "--network=slirp4netns:allow_host_loopback=false",
                "--publish=127.0.0.1::3838",
                "--read-only",
                "--user=65534:65534",
                "--cap-drop=all",
                "--security-opt=no-new-privileges",
                "--memory=512m",
                "--memory-swap=512m",
                "--pids-limit=128",
                "--tmpfs=/tmp:rw,nosuid,nodev,size=64m,mode=1777",
                "--env=HOME=/tmp",
                "--env=PROBE_INSTANCE=" + label,
                "--volume=" + str(ROOT / "experiments/session-fixture") + ":/app:ro",
                "--workdir=/app",
                "--entrypoint=Rscript",
                image["image"],
                "--vanilla",
                "-e",
                "shiny::runApp('/app',host='0.0.0.0',port=3838,launch.browser=FALSE)",
            )
            binding = command("podman", "port", name, "3838/tcp")
            target = "http://" + binding
            for _ in range(100):
                try:
                    urllib.request.urlopen(target, timeout=1).close()
                    break
                except OSError:
                    time.sleep(0.2)
            else:
                raise RuntimeError("Fixture não iniciou: " + command("podman", "logs", name))
            routes.append({"host": f"{label}.localhost:{port}", "id": label, "target": target})
        with tempfile.TemporaryDirectory(prefix="shinyd-proxy-config-") as temporary:
            config = Path(temporary) / "config.json"
            token = secrets.token_urlsafe(32)
            config.write_text(json.dumps({"token": token, "routes": routes}))
            config.chmod(0o600)
            proxy = subprocess.Popen(
                [str(proxy_path), "--config", str(config), "--bind", f"127.0.0.1:{port}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            for _ in range(50):
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=1):
                        break
                except OSError:
                    time.sleep(0.1)
            with sync_playwright() as p:
                browser = p.chromium.launch(executable_path="/usr/bin/google-chrome", headless=True)
                context = browser.new_context(accept_downloads=True)
                for route in routes:
                    context.add_cookies(
                        [
                            {
                                "name": "shinyd_probe",
                                "value": token,
                                "url": "http://" + route["host"],
                                "httpOnly": True,
                                "sameSite": "Lax",
                            }
                        ]
                    )
                pages = []
                sockets = []
                observed = []
                for index, route in enumerate(routes):
                    page = context.new_page()
                    page.on("websocket", lambda websocket: sockets.append(websocket.url))
                    page.goto("http://" + route["host"])
                    page.wait_for_function(
                        "document.querySelector('#state')?.textContent.includes('|')"
                    )
                    state = page.locator("#state").inner_text().split("|")
                    assert state[0] == route["id"] and state[1] == IMAGES[index]["r"], state
                    assert state[-1] == "0"
                    observed.append(
                        {"instance": state[0], "r": state[1], "shiny": state[2], "pid": state[3]}
                    )
                    pages.append(page)
                assert len(sockets) >= 2, sockets
                for index in [0, 1, 0]:
                    page = pages[index]
                    before = int(page.locator("#state").inner_text().split("|")[-1])
                    page.get_by_role("button", name="Somar", exact=True).click()
                    page.wait_for_function(
                        "(n) => document.querySelector('#state').textContent.endsWith('|'+n)",
                        arg=before + 1,
                    )
                    assert page.locator("#state").inner_text().startswith(routes[index]["id"] + "|")
                assert pages[0].locator("#state").inner_text().endswith("|2")
                assert pages[1].locator("#state").inner_text().endswith("|1")
                report["tests"].append(
                    {"name": "two_r_versions_and_affinity", "passed": True, "observed": observed}
                )
                pages[0].locator("input[type=file]").set_input_files(
                    {
                        "name": "fixture.txt",
                        "mimeType": "text/plain",
                        "buffer": b"upload-fixture-ok",
                    }
                )
                pages[0].wait_for_function(
                    "document.querySelector('#uploaded').textContent === 'upload-fixture-ok'"
                )
                with pages[0].expect_download() as download:
                    pages[0].get_by_role("link", name="Baixar estado").click()
                downloaded = Path(download.value.path()).read_text()
                assert downloaded.strip().startswith("a|")
                assert downloaded.strip().endswith("|2")
                report["tests"].append({"name": "upload_and_download", "passed": True})
                anonymous = browser.new_context()
                page = anonymous.new_page()
                response = page.goto("http://" + routes[0]["host"])
                assert response.status == 403
                ws_result = page.evaluate(
                    """async (address) => await new Promise(resolve => {
                    const ws = new WebSocket(address + '/websocket/');
                    ws.onopen = () => { ws.close(); resolve('opened'); };
                    ws.onerror = () => resolve('denied');
                    setTimeout(() => { ws.close(); resolve('timeout'); }, 5000);
                })""",
                    "ws://" + routes[0]["host"],
                )
                assert ws_result == "denied", ws_result
                report["tests"].append({"name": "unauthorized_http_and_websocket", "passed": True})
                # Stopping one instance must not reset the other session.
                command("podman", "stop", "--time=2", names[0])
                pages[1].get_by_role("button", name="Somar", exact=True).click()
                pages[1].wait_for_function(
                    "document.querySelector('#state').textContent.endsWith('|2')"
                )
                report["tests"].append({"name": "failure_isolation", "passed": True})
                browser.close()
    except Exception as exc:
        report["error"] = str(exc)
    finally:
        if proxy:
            proxy.terminate()
            try:
                proxy.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proxy.kill()
                proxy.wait()
        for name in names:
            subprocess.run(
                ["podman", "rm", "--force", "--ignore", name], capture_output=True, timeout=30
            )
    report["passed"] = "error" not in report
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

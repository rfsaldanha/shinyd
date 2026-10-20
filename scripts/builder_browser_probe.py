#!/usr/bin/env python3
"""Real browser probe for two builder artifacts through the executor relay."""

import argparse
import grp
import json
import os
import pwd
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from runtime_smoke import command, stop_process

ROOT = Path(__file__).resolve().parents[1]


def browse(config):
    from playwright.sync_api import sync_playwright

    if os.geteuid() == 0:
        raise RuntimeError("Chrome exige conta comum.")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path="/usr/bin/google-chrome", headless=True
        )
        try:
            context = browser.new_context(accept_downloads=True)
            observed = []
            for route in config["routes"]:
                context.add_cookies(
                    [
                        {
                            "name": "shinyd_probe",
                            "value": config["token"],
                            "url": "http://" + route["host"],
                            "httpOnly": True,
                            "sameSite": "Lax",
                        }
                    ]
                )
                page = context.new_page()
                assert page.goto("http://" + route["host"]).status == 200
                page.wait_for_function(
                    "document.querySelector('#state')?.textContent.includes('|')"
                )
                initial = page.locator("#state").inner_text().split("|")
                assert initial[0] == route["id"]
                assert page.locator("#credential_boundary").inner_text() == "credential-boundary-ok"
                assert page.locator("#environment_fingerprint").inner_text() == "environment-unset"
                page.get_by_role("button", name="Somar", exact=True).click()
                page.wait_for_function(
                    "document.querySelector('#state').textContent.endsWith('|1')"
                )
                page.locator("input[type=file]").set_input_files(
                    {"name": "probe.txt", "mimeType": "text/plain", "buffer": b"builder-upload"}
                )
                page.wait_for_function(
                    "document.querySelector('#uploaded').textContent === 'builder-upload'"
                )
                with page.expect_download() as result:
                    page.get_by_role("link", name="Baixar estado").click()
                assert Path(result.value.path()).read_text().strip().endswith("|1")
                observed.append({"r": initial[1], "shiny": initial[2]})
            print(json.dumps({"passed": True, "observed": observed}))
        finally:
            browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instances", nargs=2)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    if args.config:
        return browse(json.loads(args.config.read_text()))
    if os.geteuid() != 0 or not args.instances:
        parser.error("A preparação do proxy exige root e duas instâncias de teste.")
    uid = int(os.environ["SUDO_UID"])
    if uid == 0:
        parser.error("O usuário do navegador não pode ser root.")
    account = pwd.getpwuid(uid)
    with tempfile.TemporaryDirectory(prefix="shinyd-build-browser-") as directory:
        directory = Path(directory)
        directory.chmod(0o711)
        with socket.socket() as free:
            free.bind(("127.0.0.1", 0))
            port = free.getsockname()[1]
        config = directory / "config.json"
        config.write_text(
            json.dumps(
                {
                    "token": secrets.token_urlsafe(32),
                    "routes": [
                        {
                            "host": f"{chr(97 + i)}.localhost:{port}",
                            "id": instance,
                            "target": f"unix:/run/shinyd-runtime/apps/{instance}.sock",
                        }
                        for i, instance in enumerate(args.instances)
                    ],
                }
            )
        )
        os.chown(config, uid, grp.getgrnam("shinyd-gateway").gr_gid)
        config.chmod(0o640)
        binary = directory / "proxy"
        shutil.copyfile(ROOT / "dist/shinyd-proxy-probe", binary)
        binary.chmod(0o755)
        proxy = subprocess.Popen(
            [
                "runuser",
                "-u",
                "shinyd-control",
                "-g",
                "shinyd-gateway",
                "--",
                str(binary),
                "--config",
                str(config),
                "--bind",
                f"127.0.0.1:{port}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            for _ in range(50):
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    time.sleep(0.1)
            result = command(
                "runuser",
                "-u",
                account.pw_name,
                "--",
                sys.executable,
                str(Path(__file__).resolve()),
                "--config",
                str(config),
                timeout=90,
            )
            print(result.stdout)
        finally:
            stop_process(proxy)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Real browser, Django login and Shiny; disposable state and trusted fixtures only."""

import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs

from playwright.sync_api import sync_playwright
from shiny_session_probe import IMAGES, ROOT, free_port, start_fixture


def wait_port(port):
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("Serviço de teste não iniciou.")


def stop(process):
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def main():
    if os.geteuid() == 0:
        raise RuntimeError("Execute sem sudo; usa somente fixtures rootless confiáveis.")
    binary = ROOT / "dist/shinyd-gateway"
    if not binary.is_file():
        raise RuntimeError("Compile dist/shinyd-gateway antes de executar.")
    report = {
        "schema_version": 1,
        "observed_at": datetime.now(UTC).isoformat(),
        "scope": "Django + gateway por socket Unix + dois Shiny reais",
        "production_ready": False,
        "tests": [],
        "images": IMAGES,
        "limitations": [
            "HTTP de desenvolvimento; TLS/DNS de produção ainda não testados.",
            "Fixtures rootless confiáveis; não testa build ou rede de código enviado.",
            "Instâncias registradas pelo experimento, sem publicação pelo painel.",
        ],
    }
    names, processes, logs = [], [], []
    resources = ExitStack()
    stage = "preparação"

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        state = Path(resources.enter_context(tempfile.TemporaryDirectory(prefix="shinyd-auth-")))
        panel_port, gateway_port = free_port(), free_port()
        panel = f"http://panel.localhost:{panel_port}"
        password = secrets.token_urlsafe(32)
        env = {
            **os.environ,
            "SHINYD_STATE_DIR": str(state),
            "SHINYD_DEV": "1",
            "SHINYD_PANEL_ORIGIN": panel,
            "SHINYD_ALLOWED_HOSTS": "panel.localhost",
            "SHINYD_PROBE_PASSWORD": password,
        }
        env.pop("SHINYD_SECRET_KEY", None)

        def manage(*args):
            result = subprocess.run(
                [sys.executable, *args],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode:
                raise RuntimeError("Comando de preparação do teste falhou.")

        def sql_action(code):
            manage("manage.py", "shell", "-c", code)

        def launch(args):
            log_path = state / f"process-{len(processes)}.log"
            log = resources.enter_context(log_path.open("wb"))
            logs.append(log_path)
            process = subprocess.Popen(
                args, cwd=ROOT, env=env, stdout=log, stderr=log, start_new_session=True
            )
            processes.append(process)
            return process

        manage("scripts/bootstrap.py", "--state-dir", str(state))
        manage("manage.py", "migrate", "--noinput")
        fixtures = []
        for index, image in enumerate(IMAGES):
            label = chr(ord("a") + index)
            name = "shinyd-auth-" + uuid.uuid4().hex[:10]
            names.append(name)
            target = start_fixture(name, label, image)
            fixtures.append(
                {
                    "id": str(uuid.uuid4()),
                    "label": label,
                    "host": f"{label}.localhost:{gateway_port}",
                    "target": target,
                }
            )
        env["SHINYD_PROBE_FIXTURES"] = json.dumps(fixtures)
        sql_action("""
import json, os
from control.models import User, Application, AppGrant, AppInstance, PriorityGroup
owner = User.objects.create_superuser("owner", password=os.environ["SHINYD_PROBE_PASSWORD"])
viewer = User.objects.create_user("viewer", password=os.environ["SHINYD_PROBE_PASSWORD"])
for item in json.loads(os.environ["SHINYD_PROBE_FIXTURES"]):
    app = Application.objects.create(name="Fixture "+item["label"], slug=item["label"],
        owner=owner, priority=PriorityGroup.objects.get(slug="normal"),
        visibility="private" if item["label"] == "a" else "public")
    AppGrant.objects.create(app=app, user=viewer, role="viewer")
    AppInstance.objects.create(id=item["id"], app=app, host=item["host"],
        target=item["target"], enabled=True)
""")
        socket_path = state / "gateway.sock"
        auth_server = launch(
            [
                sys.executable,
                "-m",
                "gunicorn",
                "--chdir",
                str(ROOT / "backend"),
                "--bind",
                "unix:" + str(socket_path),
                "--umask",
                "0077",
                "--workers",
                "1",
                "--threads",
                "4",
                "shinyd_web.gateway_wsgi:application",
            ]
        )
        for _ in range(100):
            if socket_path.exists():
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("Socket do autorizador não iniciou.")
        assert socket_path.stat().st_mode & 0o077 == 0
        launch([sys.executable, "manage.py", "runserver", "--noreload", f"127.0.0.1:{panel_port}"])
        launch(
            [
                str(binary),
                "--bind",
                f"127.0.0.1:{gateway_port}",
                "--control-socket",
                str(socket_path),
                "--key-file",
                str(state / "keys/gateway.key"),
                "--panel-origin",
                panel,
                "--development-http",
            ]
        )
        wait_port(panel_port)
        wait_port(gateway_port)
        private = "http://" + fixtures[0]["host"]
        public = "http://" + fixtures[1]["host"]
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path="/usr/bin/google-chrome", headless=True)
            context = browser.new_context(accept_downloads=True)
            urls, codes, closed = [], [], []
            context.on("request", lambda request: urls.append(request.url))

            def capture_handoff(request):
                if request.url.endswith("/_shinyd/connect") and request.method == "POST":
                    codes.append(parse_qs(request.post_data)["code"][0])

            context.on("request", capture_handoff)

            def page_with_sockets(ctx):
                page = ctx.new_page()
                page.on("websocket", lambda ws: ws.on("close", lambda: closed.append(ws.url)))
                return page

            def wait_shiny(page, label):
                page.wait_for_function(
                    "(label) => document.querySelector('#state')"
                    "?.textContent.startsWith(label+'|')",
                    arg=label,
                    timeout=15000,
                )
                page.wait_for_function(
                    "document.querySelector('#credential_boundary')?.textContent "
                    "=== 'credential-boundary-ok'"
                )

            def wait_closed(page, host, before, name, started):
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline:
                    if any(host in url for url in closed[before:]):
                        elapsed = time.monotonic() - started
                        assert elapsed < 25
                        report["tests"].append(
                            {"name": name, "passed": True, "seconds": round(elapsed, 3)}
                        )
                        return
                    page.wait_for_timeout(100)
                raise RuntimeError("Conexão não foi encerrada no prazo.")

            anonymous = browser.new_context()
            denial = page_with_sockets(anonymous)
            stage = "acesso anônimo"
            assert denial.goto(private).status == 403
            assert anonymous.request.get(private + "/download/secret").status == 403
            result = denial.evaluate(
                """async address => new Promise(resolve => {
                const ws = new WebSocket(address + '/websocket/');
                ws.onopen = () => { ws.close(); resolve('opened'); };
                ws.onerror = () => resolve('denied');
                setTimeout(() => { ws.close(); resolve('timeout'); }, 3000);
            })""",
                "ws://" + fixtures[0]["host"],
            )
            assert result == "denied"
            public_page = page_with_sockets(anonymous)
            assert public_page.goto(public).status == 200
            wait_shiny(public_page, "b")
            report["tests"].append({"name": "anonymous_public_and_private_http_ws", "passed": True})

            stage = "login central"
            app_page = page_with_sockets(context)
            assert app_page.goto(private).status == 403
            app_page.get_by_role("link", name="Entrar", exact=True).click()
            app_page.get_by_label("Usuário:", exact=True).fill("viewer")
            app_page.get_by_label("Senha:", exact=True).fill(password)
            app_page.get_by_role("button", name="Entrar", exact=True).click()
            app_page.get_by_role("button", name="Abrir aplicativo", exact=True).click()
            wait_shiny(app_page, "a")
            assert app_page.url == private + "/"
            observed = []
            for page, image in zip((app_page, public_page), IMAGES, strict=True):
                fields = page.locator("#state").inner_text().split("|")
                assert fields[1] == image["r"]
                observed.append({"instance": fields[0], "r": fields[1], "shiny": fields[2]})
            report["observed_runtimes"] = observed
            cookies = context.cookies()
            panel_cookie = next(c for c in cookies if c["name"] == "shinyd_dev_session")
            app_cookie = next(c for c in cookies if c["name"] == "shinyd_dev_app")
            assert panel_cookie["domain"] == "panel.localhost"
            assert app_cookie["domain"] == "a.localhost" and app_cookie["httpOnly"]
            assert app_cookie["sameSite"] == "Lax"
            assert (
                context.request.post(
                    private + "/_shinyd/connect",
                    form={"code": codes[-1]},
                    headers={"Origin": panel},
                ).status
                == 403
            )
            report["tests"].append(
                {"name": "central_login_single_use_and_host_cookies", "passed": True}
            )

            stage = "interação e transferências"
            app_page.get_by_role("button", name="Somar", exact=True).click()
            app_page.wait_for_function(
                "document.querySelector('#state').textContent.endsWith('|1')"
            )
            app_page.locator("input[type=file]").set_input_files(
                {"name": "fixture.txt", "mimeType": "text/plain", "buffer": b"central-login-ok"}
            )
            app_page.wait_for_function(
                "document.querySelector('#uploaded').textContent === 'central-login-ok'"
            )
            with app_page.expect_download() as download:
                app_page.get_by_role("link", name="Baixar estado").click()
            data = Path(download.value.path()).read_text().strip()
            assert data.startswith("a|") and data.endswith("|1")
            report["tests"].append(
                {"name": "private_shiny_affinity_upload_download", "passed": True}
            )

            stage = "mudança público para privado"
            before, started = len(closed), time.monotonic()
            sql_action(
                "from control.models import Application; "
                "Application.objects.filter(slug='b').update(visibility='private')"
            )
            wait_closed(
                public_page, fixtures[1]["host"], before, "public_to_private_closes_ws", started
            )
            assert anonymous.request.get(public).status == 403
            assert (
                context.request.get(
                    public, headers={"Cookie": "shinyd_dev_app=" + app_cookie["value"]}
                ).status
                == 403
            )
            report["tests"].append({"name": "credential_cannot_cross_instances", "passed": True})

            stage = "revogação de permissão"
            before, started = len(closed), time.monotonic()
            sql_action(
                "from control.models import AppGrant; "
                "AppGrant.objects.filter(app__slug='a', user__username='viewer').delete()"
            )
            wait_closed(
                app_page, fixtures[0]["host"], before, "grant_revocation_closes_ws", started
            )
            assert context.request.get(private).status == 403

            stage = "logout"
            sql_action("""
from control.models import AppGrant, Application, User
AppGrant.objects.create(app=Application.objects.get(slug="a"),
    user=User.objects.get(username="viewer"), role="viewer")
""")
            app_page.goto(panel + "/connect/" + fixtures[0]["id"] + "/")
            app_page.get_by_role("button", name="Abrir aplicativo", exact=True).click()
            wait_shiny(app_page, "a")
            panel_page = context.new_page()
            panel_page.goto(panel)
            before, started = len(closed), time.monotonic()
            panel_page.get_by_role("button", name="Sair", exact=True).click()
            wait_closed(app_page, fixtures[0]["host"], before, "central_logout_closes_ws", started)
            assert context.request.get(private).status == 403

            stage = "falha do autorizador"
            panel_page.get_by_label("Usuário:", exact=True).fill("viewer")
            panel_page.get_by_label("Senha:", exact=True).fill(password)
            panel_page.get_by_role("button", name="Entrar", exact=True).click()
            app_page.goto(panel + "/connect/" + fixtures[0]["id"] + "/")
            app_page.get_by_role("button", name="Abrir aplicativo", exact=True).click()
            wait_shiny(app_page, "a")
            before, started = len(closed), time.monotonic()
            # Kill only the test's dedicated process group, including Gunicorn
            # workers; graceful shutdown can continue serving existing connections.
            os.killpg(auth_server.pid, signal.SIGKILL)
            auth_server.wait(timeout=5)
            assert context.request.get(private).status == 503
            wait_closed(
                app_page, fixtures[0]["host"], before, "authorizer_outage_closes_ws", started
            )
            assert context.request.get(private).status == 503
            stage = "ausência de credenciais em URLs e logs"
            sensitive = [password, (state / "keys/gateway.key").read_text(), *codes]
            sensitive += [c["value"] for c in context.cookies()]
            observed = "\n".join(urls) + "\n".join(path.read_text() for path in logs)
            assert all(value not in observed for value in sensitive)
            report["tests"].append(
                {"name": "credentials_absent_from_urls_logs_and_app", "passed": True}
            )
            browser.close()
    except (Exception, KeyboardInterrupt) as exc:
        # Browser exceptions can contain form values. Report only stage and class.
        report["error"] = {"stage": stage, "type": type(exc).__name__}
    finally:
        for process in reversed(processes):
            stop(process)
        for name in names:
            result = subprocess.run(
                ["podman", "rm", "--force", "--ignore", name], capture_output=True, timeout=30
            )
            if result.returncode:
                report["cleanup_error"] = "Falha ao remover container de fixture."
        resources.close()
        signal.signal(signal.SIGTERM, previous)
    report["passed"] = "error" not in report and "cleanup_error" not in report
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

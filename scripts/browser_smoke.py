#!/usr/bin/env python3
"""Exercise the real UI against disposable state; never use the operator's DB."""

import argparse
import io
import os
import pwd
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from contextlib import ExitStack
from pathlib import Path

from agent_probe import start_agent, stop_agent
from playwright.sync_api import sync_playwright

repo = Path(__file__).resolve().parents[1]
evidence = Path("/tmp/shinyd-evidence")
evidence.mkdir(exist_ok=True)


def run(*args, env):
    subprocess.run(
        [sys.executable, *args], cwd=repo, env=env, check=True, stdout=subprocess.DEVNULL
    )


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--agent-binary", type=Path, help="Inclui diagnóstico com agente real")
args = parser.parse_args()

with tempfile.TemporaryDirectory(prefix="shinyd-browser-") as state, ExitStack() as resources:
    env = {**os.environ, "SHINYD_DEV": "1", "SHINYD_STATE_DIR": state}
    agent = None
    if args.agent_binary:
        listener = resources.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
        listener.bind(str(Path(state) / "agent.sock"))
        listener.listen(32)
        log = resources.enter_context((Path(state) / "agent.log").open("wb"))
        agent = start_agent(
            args.agent_binary, listener, pwd.getpwuid(os.getuid()).pw_name, state, log
        )
        resources.callback(stop_agent, agent)
        env.update(
            SHINYD_AGENT_SOCKET=listener.getsockname(), SHINYD_AGENT_DEV_UID=str(os.getuid())
        )
    run("scripts/bootstrap.py", "--state-dir", state, env=env)
    run("manage.py", "migrate", "--noinput", env=env)
    password = secrets.token_urlsafe(32)
    run(
        "manage.py",
        "createsuperuser",
        "--noinput",
        "--username",
        "browser-admin",
        "--email",
        "test@example.invalid",
        env={**env, "DJANGO_SUPERUSER_PASSWORD": password},
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    server = subprocess.Popen(
        [sys.executable, "manage.py", "runserver", "--noreload", f"127.0.0.1:{port}"],
        cwd=repo,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(base + "/healthz", timeout=1).close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("Servidor de teste não iniciou")
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path="/usr/bin/google-chrome", headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base)
            page.get_by_label("Usuário:", exact=True).fill("browser-admin")
            page.get_by_label("Senha:", exact=True).fill(password)
            page.get_by_role("button", name="Entrar", exact=True).click()
            page.get_by_role("heading", name="Seus aplicativos").wait_for()
            page.screenshot(path=str(evidence / "dashboard-desktop.png"), full_page=True)
            if agent:
                page.get_by_role("link", name="Diagnóstico do servidor", exact=True).click()
                page.get_by_role("heading", name="Agente conectado").wait_for()
                assert page.locator(".diagnostic-check").count() >= 15
                assert "Consultas somente de leitura" in page.locator("#main").inner_text()
                page.screenshot(path=str(evidence / "agent-desktop.png"), full_page=True)
                page.set_viewport_size({"width": 360, "height": 800})
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                page.screenshot(path=str(evidence / "agent-mobile.png"), full_page=True)
                page.set_viewport_size({"width": 1440, "height": 1000})
                page.goto(base)
            page.get_by_role("link", name="+ Novo aplicativo", exact=True).click()
            page.get_by_label("Nome:", exact=True).fill("Indicadores de demonstração")
            page.get_by_label("Endereço:", exact=True).fill("indicadores")
            page.get_by_label("Descrição:", exact=True).fill(
                "Fixture descartável do teste de navegador. Não é um app publicado."
            )
            page.get_by_role("button", name="Salvar", exact=True).click()
            page.get_by_role("link", name="Enviar ZIP", exact=True).click()
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, "w") as z:
                z.writestr("app.R", "# Fixture: somente inspecionada")
            page.get_by_label("Aplicativo em ZIP:", exact=True).set_input_files(
                {"name": "fixture.zip", "mimeType": "application/zip", "buffer": archive.getvalue()}
            )
            page.get_by_role("button", name="Salvar", exact=True).click()
            page.get_by_role("heading", name="Na fila", exact=True).wait_for()
            run("manage.py", "worker", "--once", env=env)
            # HTMX must update the page without a manual reload.
            page.get_by_role("heading", name="Concluído", exact=True).wait_for(timeout=10000)
            assert "Nenhum código foi executado" in page.locator("#job-status").inner_text()
            page.screenshot(path=str(evidence / "inspection-desktop.png"), full_page=True)
            page.get_by_role("link", name="Voltar ao app", exact=True).click()
            page.get_by_role("link", name="Histórico de versões e publicações", exact=True).click()
            assert page.locator("tr[data-version]").count() == 1
            assert "Validado" in page.locator("#versions").inner_text()
            assert "Nenhuma publicação" in page.locator("#publications").inner_text()
            page.screenshot(path=str(evidence / "history-desktop.png"), full_page=True)
            page.set_viewport_size({"width": 360, "height": 800})
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.screenshot(path=str(evidence / "history-mobile.png"), full_page=True)
            page.goto(base + "/operations/")
            page.get_by_label("Operação:", exact=True).select_option("stop")
            page.get_by_label("Todos os aplicativos sob minha gestão neste momento").check()
            page.get_by_role("button", name="Revisar impacto", exact=True).click()
            page.get_by_role("heading", name="Confirmar: Parar", exact=True).wait_for()
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.screenshot(path=str(evidence / "operations-mobile.png"), full_page=True)
            page.get_by_label("Entendo o impacto sobre as conexões", exact=False).check()
            page.get_by_role("button", name="Confirmar operação", exact=True).click()
            # This disposable unprivileged panel intentionally has execution disabled.
            page.get_by_text("Não enviado", exact=True).wait_for()
            assert "Ativação desabilitada" in page.locator("#operation-status").inner_text()
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.goto(base)
            page.set_viewport_size({"width": 360, "height": 800})
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.screenshot(path=str(evidence / "dashboard-mobile.png"), full_page=True)
            if agent:
                stop_agent(agent)
                listener.close()
                page.goto(base + "/server/diagnostics/")
                page.get_by_role("heading", name="Diagnóstico indisponível").wait_for()
                page.goto(base)
                page.get_by_role("heading", name="Seus aplicativos").wait_for()
                print("Agente: diagnóstico real, viewport 360px e queda do serviço passaram.")
            assert not errors, errors
            browser.close()
            print(
                "Browser smoke: login, cadastro, upload, worker, polling, histórico "
                "e viewport 360px passaram."
            )
            print(f"Capturas: {evidence}")
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()

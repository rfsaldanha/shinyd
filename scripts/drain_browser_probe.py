"""Drain/cancel/resume using actual Shiny sessions in the installed fixture."""

from pathlib import Path
from urllib.parse import urlsplit


def probe(page, app_url, other_url, data, value, run, check, open_app):
    previous = open_app(app_url)
    origin = urlsplit(page.url)._replace(path="", query="", fragment="").geturl()
    state = page.locator("#state").inner_text().split("|")
    panel = page.context.new_page()
    other = page.context.new_page()
    other.goto(other_url)
    other.get_by_role("link", name="Abrir aplicativo", exact=True).click()
    other.get_by_role("button", name="Abrir aplicativo", exact=True).click()
    other.wait_for_function("document.querySelector('#state')?.textContent.split('|').length === 5")
    other_state = other.locator("#state").inner_text().split("|")

    def increment(target, before):
        expected = [*before[:4], str(int(before[4]) + 1)]
        target.get_by_role("button", name="Somar", exact=True).click()
        target.wait_for_function(
            "expected => document.querySelector('#state').textContent === expected",
            arg="|".join(expected),
        )
        return expected

    def wait_for_drain():
        panel.goto(app_url)
        panel.get_by_role("link", name="Aplicar configuração", exact=True).click()
        panel.get_by_label("Modo de troca:", exact=True).select_option("drain")
        panel.get_by_label("Entendo o impacto do modo de troca", exact=False).check()
        panel.get_by_role("button", name="Aplicar configuração e reiniciar", exact=True).click()
        panel.get_by_text("waiting_connections", exact=True).wait_for(timeout=180000)

    panel.goto(app_url)
    panel.get_by_role("link", name="Adicionar variável ou segredo", exact=True).click()
    panel.get_by_label("Nome:", exact=True).fill("DATA_REVISION")
    panel.get_by_label("Novo valor:", exact=True).fill("fixture-drained")
    panel.get_by_role("button", name="Salvar", exact=True).click()
    wait_for_drain()
    state = increment(page, state)
    check("drain_existing_shiny_session_keeps_identity_and_reactivity")
    outcome = page.evaluate(
        """() => new Promise(resolve => {
        const ws = new WebSocket(location.origin.replace('https:', 'wss:')+'/websocket/');
        const timer = setTimeout(()=>{ws.close();resolve('timeout')},5000);
        ws.onopen=()=>{clearTimeout(timer);ws.close();resolve('opened')};
        ws.onerror=()=>{clearTimeout(timer);resolve('denied')};
        })"""
    )
    check("drain_new_authorized_websocket_denied", outcome == "denied")
    page.locator("input[type=file]").set_input_files(
        {"name": "during-drain.txt", "mimeType": "text/plain", "buffer": b"drain-upload-ok"}
    )
    page.wait_for_function("document.querySelector('#uploaded')?.textContent === 'drain-upload-ok'")
    with page.expect_download() as download:
        page.get_by_role("link", name="Baixar estado").click()
    check(
        "drain_existing_session_upload_download",
        Path(download.value.path()).read_text().strip() == "|".join(state),
    )
    panel.get_by_role("button", name="Solicitar cancelamento", exact=True).click()
    panel.get_by_role("heading", name="Cancelado", exact=True).wait_for(timeout=180000)
    newcomer = page.context.new_page()
    newcomer.goto(origin)
    newcomer.wait_for_function(
        "document.querySelector('#state')?.textContent.split('|').length === 5"
    )
    check(
        "drain_cancel_reopens_previous",
        newcomer.locator("#state").inner_text().split("|")[0] == previous,
    )
    newcomer.close()
    state = increment(page, state)
    wait_for_drain()
    run("systemctl", "restart", "shinyd-worker.service")
    state = increment(page, state)
    check("drain_worker_restart_preserves_session_and_wait")
    # Navigating away closes the live WebSocket, without destroying the browser context.
    page.goto(app_url)
    panel.get_by_role("heading", name="Concluído", exact=True).wait_for(timeout=240000)
    current = open_app(app_url)
    page.wait_for_function(
        "document.querySelector('#data_revision')?.textContent === 'fixture-drained'"
    )
    page.wait_for_function(
        "v => document.querySelector('#data_state')?.textContent === v", arg=value
    )
    check(
        "drain_switch_after_last_connection_preserves_data",
        current != previous and (data / "fixture.txt").read_text().strip() == value,
    )
    check(
        "drain_previous_writer_removed",
        run("podman", "inspect", "shinyd-" + previous, check=False).returncode != 0,
    )
    increment(other, other_state)
    check("drain_preserves_other_app_session")
    panel.goto(app_url)
    panel.set_viewport_size({"width": 360, "height": 800})
    check(
        "drain_telemetry_mobile",
        panel.evaluate("document.documentElement.scrollWidth <= innerWidth")
        and "WebSocket abertas" in panel.locator("#connections").inner_text(),
    )
    other.close()
    panel.close()
    return current

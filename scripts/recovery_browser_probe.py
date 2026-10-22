"""Crash real disposable Shiny processes and verify the durable recovery circuit."""

import json
import time
import uuid
from urllib.parse import urlsplit


def probe(page, app_url, other_url, data, value, run, admin, check, open_app):
    app_id = str(uuid.UUID(urlsplit(app_url).path.strip("/").split("/")[-1]))
    other_id = str(uuid.UUID(urlsplit(other_url).path.strip("/").split("/")[-1]))
    origin = urlsplit(app_url)._replace(path="", query="", fragment="").geturl()
    panel, other = page.context.new_page(), page.context.new_page()

    def operation(action, identity):
        panel.goto(origin + "/operations/")
        panel.get_by_label("Operação:", exact=True).select_option(action)
        panel.get_by_label("Modo de parada ou reinício:", exact=True).select_option("immediate")
        panel.locator(f'input[name="apps"][value="{identity}"]').check()
        panel.get_by_role("button", name="Revisar impacto", exact=True).click()
        panel.get_by_label("Entendo o impacto sobre as conexões", exact=False).check()
        panel.get_by_role("button", name="Confirmar operação", exact=True).click()
        panel.get_by_text(
            "Processamento encerrado. Confira o resultado de cada aplicativo.", exact=True
        ).wait_for(timeout=240000)
        if "Concluído" not in panel.locator(".operation-result").inner_text():
            raise AssertionError("recovery_fixture_manual_operation")

    def configure(enabled):
        panel.goto(app_url)
        panel.get_by_role("link", name="Configurar recuperação", exact=True).click()
        panel.get_by_label(
            "Recuperar automaticamente após encerramento do processo", exact=False
        ).set_checked(enabled)
        panel.get_by_label("Entendo que as sessões encerradas", exact=False).check()
        panel.get_by_role("button", name="Salvar", exact=True).click()
        panel.get_by_role("heading", name="Recuperação de falhas", exact=True).wait_for()

    def state():
        output = admin(
            "import json\nfrom control.models import RecoveryState, Job\n"
            f"r=RecoveryState.objects.get(app_id='{app_id}')\n"
            "print('RECOVERY='+json.dumps({'state':r.state,'attempts':r.attempts,"
            "'active':str(r.app.active_publication_id),'job':str(r.job_id),"
            "'jobs':Job.objects.filter(app=r.app,operation='recover').count()}))\n"
        )
        return json.loads(
            next(
                line.removeprefix("RECOVERY=")
                for line in output.splitlines()
                if line.startswith("RECOVERY=")
            )
        )

    def wait(predicate, timeout=240):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = state()
            if predicate(result):
                return result
            time.sleep(2)
        raise AssertionError("recovery_fixture_deadline")

    operation("start", other_id)
    other.goto(other_url)
    other.get_by_role("link", name="Abrir aplicativo", exact=True).click()
    other.get_by_role("button", name="Abrir aplicativo", exact=True).click()
    other.wait_for_function("document.querySelector('#state')?.textContent.split('|').length === 5")
    other_state = other.locator("#state").inner_text().split("|")
    configure(True)
    check("recovery_enabled_through_browser")
    current = open_app(app_url)
    for attempt in range(1, 4):
        old_origin = urlsplit(page.url)._replace(path="", query="", fragment="").geturl()
        run("podman", "kill", "--signal", "KILL", "shinyd-" + current)
        waiting = wait(lambda s: s["state"] == "waiting", timeout=60)
        check(f"recovery_backoff_{attempt}", waiting["attempts"] == attempt - 1)
        if attempt == 2:
            run("systemctl", "restart", "shinyd-worker.service")
            check("recovery_worker_restart_keeps_counter", state()["attempts"] == 1)
        recovered = wait(
            lambda s, previous=current: s["state"] == "healthy" and s["active"] != previous
        )
        previous, current = current, open_app(app_url)
        page.wait_for_function(
            "v => document.querySelector('#data_state')?.textContent === v", arg=value
        )
        check(
            f"recovery_new_process_and_data_{attempt}",
            current == recovered["active"]
            and recovered["attempts"] == attempt
            and (data / "fixture.txt").read_text().strip() == value,
        )
        check(
            f"recovery_old_writer_removed_{attempt}",
            run("podman", "inspect", "shinyd-" + previous, check=False).returncode != 0,
        )
        route = page.context.new_page()
        try:
            check(f"recovery_old_route_revoked_{attempt}", route.goto(old_origin).status == 404)
        finally:
            route.close()
        other_state[4] = str(int(other_state[4]) + 1)
        other.get_by_role("button", name="Somar", exact=True).click()
        other.wait_for_function(
            "v => document.querySelector('#state')?.textContent === v", arg="|".join(other_state)
        )
        check(f"recovery_preserves_other_session_{attempt}")

    run("podman", "kill", "--signal", "KILL", "shinyd-" + current)
    opened = wait(lambda s: s["state"] == "open", timeout=60)
    check("recovery_circuit_opens_after_three", opened["attempts"] == opened["jobs"] == 3)
    run("systemctl", "restart", "shinyd-worker.service")
    time.sleep(15)
    check("recovery_open_circuit_survives_worker_restart", state() == opened)
    panel.goto(app_url)
    check(
        "recovery_circuit_visible_and_no_open_link",
        "Recuperação suspensa após três" in panel.locator("#recovery-status").inner_text()
        and panel.get_by_role("link", name="Abrir aplicativo", exact=True).count() == 0,
    )
    panel.set_viewport_size({"width": 360, "height": 800})
    check(
        "recovery_mobile_no_overflow",
        panel.evaluate("document.documentElement.scrollWidth <= window.innerWidth"),
    )
    configure(False)
    operation("restart", app_id)
    resumed = open_app(app_url)
    check(
        "recovery_manual_restart_preserves_data",
        resumed != current and (data / "fixture.txt").read_text().strip() == value,
    )
    operation("stop", other_id)
    panel.close()
    other.close()
    return resumed

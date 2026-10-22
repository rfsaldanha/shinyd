"""Installed lifecycle operations against real Shiny sessions and persistent data."""

import uuid
from urllib.parse import urlsplit


def probe(page, app_url, other_url, data, value, run, check, open_app):
    panel_origin = urlsplit(app_url)._replace(path="", query="", fragment="").geturl()
    app_id = str(uuid.UUID(urlsplit(app_url).path.strip("/").split("/")[-1]))
    other_id = str(uuid.UUID(urlsplit(other_url).path.strip("/").split("/")[-1]))
    panel, other = page.context.new_page(), page.context.new_page()

    def open_other():
        other.goto(other_url)
        other.get_by_role("link", name="Abrir aplicativo", exact=True).click()
        other.get_by_role("button", name="Abrir aplicativo", exact=True).click()
        other.wait_for_function(
            "document.querySelector('#state')?.textContent.split('|').length === 5"
        )
        return other.locator("#state").inner_text().split("|")

    def reactive(target, before):
        expected = "|".join([*before[:4], str(int(before[4]) + 1)])
        target.get_by_role("button", name="Somar", exact=True).click()
        target.wait_for_function(
            "v => document.querySelector('#state')?.textContent === v", arg=expected
        )
        return expected.split("|")

    def submit(action, mode="immediate", ids=None, all_apps=False):
        panel.goto(panel_origin + "/operations/")
        panel.get_by_label("Operação:", exact=True).select_option(action)
        panel.get_by_label("Modo de parada ou reinício:", exact=True).select_option(mode)
        if all_apps:
            panel.get_by_label("Todos os aplicativos sob minha gestão neste momento").check()
        else:
            for identity in ids or [app_id]:
                panel.locator(f'input[name="apps"][value="{identity}"]').check()
        panel.get_by_role("button", name="Revisar impacto", exact=True).click()
        panel.get_by_role("heading", name="Confirmar:", exact=False).wait_for()
        panel.get_by_label("Entendo o impacto sobre as conexões", exact=False).check()
        panel.get_by_role("button", name="Confirmar operação", exact=True).click()
        panel.locator("#operation-status").wait_for()

    def completed(count):
        panel.get_by_text(
            "Processamento encerrado. Confira o resultado de cada aplicativo.", exact=True
        ).wait_for(timeout=240000)
        check(
            "lifecycle_results_completed_" + str(len(checks)),
            panel.locator(".operation-result").count() == count
            and all(
                "Concluído" in text for text in panel.locator(".operation-result").all_inner_texts()
            ),
        )
        checks.append(count)

    checks = []
    previous = open_app(app_url)
    old_origin = urlsplit(page.url)._replace(path="", query="", fragment="").geturl()
    other_state = open_other()
    submit("restart")
    completed(1)
    page.wait_for_function("!Shiny.shinyapp.$socket || Shiny.shinyapp.$socket.readyState !== 1")
    current = open_app(app_url)
    check(
        "lifecycle_restart_new_instance_same_code_and_data",
        current != previous and (data / "fixture.txt").read_text().strip() == value,
    )
    check("lifecycle_old_route_closed", page.context.request.get(old_origin).status == 404)
    other_state = reactive(other, other_state)
    check("lifecycle_individual_restart_preserves_other_session")
    submit("stop", "drain")
    operation_url = panel.url
    panel.get_by_role("link", name="Acompanhar tarefa", exact=True).click()
    panel.get_by_text("waiting_connections", exact=True).wait_for(timeout=30000)
    state = page.locator("#state").inner_text().split("|")
    reactive(page, state)
    run("systemctl", "restart", "shinyd-worker.service")
    page.goto(app_url)
    panel.goto(operation_url)
    completed(1)
    check(
        "lifecycle_drained_stop_survives_worker_restart",
        run("podman", "inspect", "shinyd-" + current, check=False).returncode != 0,
    )
    page.reload()
    check(
        "lifecycle_stopped_app_not_opened_on_visit",
        page.get_by_role("link", name="Abrir aplicativo", exact=True).count() == 0,
    )
    other_state = reactive(other, other_state)
    check("lifecycle_individual_stop_preserves_other_session")

    submit("start", ids=[app_id, other_id])
    completed(2)
    check(
        "lifecycle_start_running_app_is_noop",
        "App já iniciado" in panel.locator("#operation-status").inner_text(),
    )
    current = open_app(app_url)
    page.wait_for_function(
        "v => document.querySelector('#data_state')?.textContent === v", arg=value
    )
    page.wait_for_function(
        "document.querySelector('#data_revision')?.textContent === 'fixture-drained'"
    )
    check("lifecycle_start_restores_version_data_and_current_configuration")
    other_state = reactive(other, other_state)

    submit("restart", "drain", ids=[app_id, other_id])
    # Close both sessions so each queued job can drain independently.
    page.goto(app_url)
    other.goto(other_url)
    completed(2)
    restarted = open_app(app_url)
    restarted_other = open_other()
    check(
        "lifecycle_batch_restart_replaces_both_instances",
        restarted != current and restarted_other[0] != other_state[0],
    )
    check(
        "lifecycle_batch_restart_preserves_data",
        (data / "fixture.txt").read_text().strip() == value,
    )

    submit("stop", all_apps=True)
    completed(2)
    check(
        "lifecycle_stop_all_removes_both_instances",
        all(
            run("podman", "inspect", "shinyd-" + identity, check=False).returncode != 0
            for identity in [restarted, restarted_other[0]]
        ),
    )
    page.goto(app_url)
    other.goto(other_url)
    submit("start")
    completed(1)
    panel.set_viewport_size({"width": 360, "height": 800})
    check(
        "lifecycle_results_mobile_no_overflow",
        panel.evaluate("document.documentElement.scrollWidth <= window.innerWidth"),
    )
    panel.close()
    other.close()
    return open_app(app_url)

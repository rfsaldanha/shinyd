"""Rollback through the browser, preserving the current environment and live data."""

import json
from urllib.parse import urlsplit


def probe(page, app_url, app_id, other_url, data, value, run, admin, check, open_app):
    def inventory():
        output = admin(
            "import json\nfrom control.models import Application, Job, Publication, Version\n"
            f"a=Application.objects.get(pk='{app_id}')\n"
            "p=a.active_publication\n"
            "s=Publication.objects.filter(app=a,status='retired').order_by('created_at').first()\n"
            "print('ROLLBACK='+json.dumps({'active':str(p.pk),'version':str(p.version_id),"
            "'image':p.version.image_digest,'config':p.config_revision,"
            "'restored_from':str(p.restored_from_id),'source':str(s.pk),"
            "'source_version':str(s.version_id),'source_image':s.version.image_digest,"
            "'source_config':s.config_revision,"
            "'versions':Version.objects.filter(app=a).count(),"
            "'builds':Job.objects.filter(app=a,kind='prepare_version').count()}))\n"
        )
        return json.loads(
            next(
                line.removeprefix("ROLLBACK=")
                for line in output.splitlines()
                if line.startswith("ROLLBACK=")
            )
        )

    before = inventory()
    check(
        "rollback_source_has_older_code_and_configuration",
        before["source_version"] != before["version"]
        and before["source_config"] < before["config"],
    )
    other_page = page.context.new_page()
    other_page.goto(other_url)
    other_page.get_by_role("link", name="Abrir aplicativo", exact=True).click()
    other_page.get_by_role("button", name="Abrir aplicativo", exact=True).click()
    other_page.wait_for_function(
        "document.querySelector('#state')?.textContent.split('|').length === 5"
    )
    other_state = other_page.locator("#state").inner_text().split("|")

    def confirm():
        page.get_by_label("Entendo o impacto do modo de troca", exact=False).check()
        page.get_by_label("Entendo que voltar o código", exact=False).check()
        page.get_by_role("button", name="Confirmar retorno à versão", exact=True).click()

    page.goto(app_url)
    page.get_by_role("link", name="Histórico de versões e publicações", exact=True).click()
    history_url = page.url
    page.set_viewport_size({"width": 360, "height": 800})
    check(
        "rollback_history_mobile",
        page.evaluate("document.documentElement.scrollWidth <= innerWidth"),
    )
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.locator(f'[data-publication="{before["source"]}"]').get_by_role(
        "link", name="Voltar a esta versão", exact=True
    ).click()
    confirmation_url = page.url
    # Change the pending configuration from another tab after opening confirmation.
    editor = page.context.new_page()
    editor.goto(app_url)
    editor.get_by_role("link", name="Adicionar variável ou segredo", exact=True).click()
    editor.get_by_label("Nome:", exact=True).fill("DATA_REVISION")
    editor.get_by_label("Novo valor:", exact=True).fill("fixture-v3")
    editor.get_by_role("button", name="Salvar", exact=True).click()
    editor.close()
    confirm()
    check(
        "rollback_stale_confirmation_denied",
        "O aplicativo ou sua configuração mudou" in page.locator("#main").inner_text()
        and inventory()["active"] == before["active"]
        and (data / "fixture.txt").read_text().strip() == value,
    )
    page.goto(confirmation_url)
    confirm()
    page.get_by_role("heading", name="Concluído", exact=True).wait_for(timeout=240000)
    after = inventory()
    check(
        "rollback_reuses_image_without_upload_or_build",
        after["version"] == before["source_version"]
        and after["image"] == before["source_image"]
        and after["versions"] == before["versions"]
        and after["builds"] == before["builds"],
    )
    check(
        "rollback_new_publication_auditable",
        after["active"] not in {before["source"], before["active"]}
        and after["restored_from"] == before["source"]
        and after["config"] == before["config"] + 1,
    )
    restored = open_app(app_url)
    origin = urlsplit(page.url)._replace(path="", query="", fragment="").geturl()
    page.wait_for_function(
        "value => document.querySelector('#data_state')?.textContent === value", arg=value
    )
    page.wait_for_function("document.querySelector('#data_revision')?.textContent === 'fixture-v3'")
    page.wait_for_function(
        "document.querySelector('#environment_fingerprint')?.textContent === 'environment-unset'"
    )
    check("rollback_keeps_data_and_pending_configuration", restored == after["active"])
    check("rollback_removed_secret_not_restored")
    check(
        "rollback_previous_writer_removed",
        run("podman", "inspect", "shinyd-" + before["active"], check=False).returncode != 0,
    )
    old_origin = origin.replace(
        "p" + restored.replace("-", ""), "p" + before["active"].replace("-", "")
    )
    check("rollback_previous_route_revoked", page.goto(old_origin).status == 404)
    other_page.get_by_role("button", name="Somar", exact=True).click()
    other_page.wait_for_function(
        "expected => document.querySelector('#state').textContent === expected",
        arg="|".join([*other_state[:4], str(int(other_state[4]) + 1)]),
    )
    check("rollback_preserves_other_app_session")
    other_page.close()
    page.goto(history_url)
    row = page.locator(f'[data-publication="{after["active"]}"]')
    check(
        "rollback_history_records_source",
        before["source"] in row.inner_text() and "Ativa" in row.inner_text(),
    )
    open_app(app_url)
    return restored

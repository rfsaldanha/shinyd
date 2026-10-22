"""Real volume checks for the installed, disposable fixture only."""

import hashlib
import json
import uuid
import zipfile
from pathlib import Path
from urllib.parse import urlsplit


def probe(page, app_url, fixture, temporary, run, admin, check, created, data_apps):
    app_id = str(uuid.UUID(urlsplit(app_url).path.strip("/").split("/")[-1]))
    data_apps.append(app_id)
    data = Path("/var/lib/shinyd-data") / app_id / "files"
    key = Path("/var/lib/shinyd-control/keys/secrets.key")
    key_before = hashlib.sha256(key.read_bytes()).digest(), key.stat().st_uid, key.stat().st_mode

    def enable():
        page.get_by_role("link", name="Habilitar dados persistentes", exact=True).click()
        page.get_by_label("Entendo que as próximas ativações", exact=False).check()
        page.get_by_role("button", name="Habilitar dados persistentes", exact=True).click()

    def activate(button="Ativar versão", success=True):
        page.get_by_label("Entendo o impacto do modo de troca", exact=False).check()
        page.get_by_role("button", name=button, exact=True).click()
        page.get_by_role("heading", name="Concluído" if success else "Falhou", exact=True).wait_for(
            timeout=240000
        )

    def open_app(url):
        page.goto(url)
        page.get_by_role("link", name="Abrir aplicativo", exact=True).click()
        page.get_by_role("button", name="Abrir aplicativo", exact=True).click()
        page.wait_for_function(
            "document.querySelector('#state')?.textContent.split('|').length === 5"
        )
        return page.locator("#state").inner_text().split("|")[0]

    def expect_data(value):
        page.wait_for_function(
            "value => document.querySelector('#data_state')?.textContent === value", arg=value
        )

    def publish(path, *, success=True):
        page.get_by_role("link", name="Enviar ZIP", exact=True).click()
        page.get_by_label("Aplicativo em ZIP:", exact=True).set_input_files(str(path))
        page.get_by_role("button", name="Salvar", exact=True).click()
        page.get_by_role("heading", name="Concluído", exact=True).wait_for(timeout=90000)
        page.get_by_role("link", name="Preparar versão", exact=True).click()
        page.get_by_label("Ambiente R aprovado:", exact=True).select_option("r-a")
        page.get_by_role("button", name="Salvar", exact=True).click()
        page.get_by_role("heading", name="Concluído", exact=True).wait_for(timeout=180000)
        page.get_by_role("link", name="Ativar versão", exact=True).click()
        activate(success=success)

    def archive(path, prefix):
        with zipfile.ZipFile(fixture) as source, zipfile.ZipFile(path, "w") as target:
            for name in source.namelist():
                content = source.read(name)
                if name == "project/app.R":
                    content = prefix.encode() + content
                target.writestr(name, content)

    page.goto(app_url)
    enable()
    page.get_by_role("link", name="Aplicar persistência e reiniciar", exact=True).click()
    activate("Aplicar configuração e reiniciar")
    first = open_app(app_url)
    expect_data("data-empty")
    check("data_enabled_through_browser")
    value = "persistent-" + uuid.uuid4().hex
    page.get_by_label("Dado persistente", exact=True).fill(value)
    page.get_by_role("button", name="Gravar dado", exact=True).click()
    expect_data(value)
    metadata = json.loads(run("podman", "inspect", "shinyd-" + first).stdout)[0]
    check(
        "data_mapped_write_and_private_parent",
        (data / "fixture.txt").read_text().strip() == value
        and (data / "fixture.txt").stat().st_uid == 65534
        and data.parent.stat().st_uid == 0
        and data.parent.stat().st_mode & 0o077 == 0,
    )
    run(
        "podman",
        "exec",
        "shinyd-" + first,
        "Rscript",
        "--vanilla",
        "-e",
        'stopifnot(file.symlink("/var/lib/shinyd-control/keys/secrets.key","/data/escape"));'
        'stopifnot(!file.exists("/data/escape"),!dir.exists("/var/lib/shinyd-data"),'
        'file.access("/opt/shinyd/launch.R",2)!=0)',
    )
    check("data_cannot_reach_control_or_host_parent")
    contender = str(uuid.uuid4())
    created.append(contender)  # Cleanup even if the negative test unexpectedly creates it.
    outcome = admin(
        "from control.models import Application\n"
        "from control import runtime_client\n"
        "from control.environment import reveal\n"
        f"p=Application.objects.get(pk='{app_id}').active_publication\n"
        "try:\n"
        f" runtime_client.request('PUT','{contender}',p.runtime_spec,environment=reveal(p))\n"
        "except runtime_client.RuntimeRejected as exc:\n"
        " print('REFUSAL='+exc.code)\n"
    )
    check("data_second_writer_denied", "REFUSAL=data_in_use" in outcome)

    failed = temporary / "data-failed.zip"
    archive(
        failed,
        'writeLines("probe-only",file.path(Sys.getenv("SHINYD_DATA_DIR"),"probe-only"));'
        'stop("fixture failure")\n',
    )
    page.goto(app_url)
    publish(failed, success=False)
    check(
        "data_failed_probe_preserves_live_volume_and_process",
        not (data / "probe-only").exists()
        and (data / "fixture.txt").read_text().strip() == value
        and json.loads(run("podman", "inspect", "shinyd-" + first).stdout)[0]["State"]["Pid"]
        == metadata["State"]["Pid"],
    )
    check("data_failed_probe_keeps_previous_route", open_app(app_url) == first)
    expect_data(value)

    replacement = temporary / "data-next.zip"
    archive(replacement, "# A second source version for persistent data verification.\n")
    page.goto(app_url)
    publish(replacement)
    second = open_app(app_url)
    expect_data(value)
    check("data_survives_new_code_publication", first != second)
    run(
        "podman",
        "exec",
        "shinyd-" + second,
        "Rscript",
        "--vanilla",
        "-e",
        'stopifnot(!file.exists("/tmp/fixture-persistence-marker"),'
        '!file.exists("/data/escape"),nzchar(Sys.readlink("/data/escape")))',
    )
    check("data_temporary_files_discarded_and_symlink_not_followed")
    check(
        "data_control_key_unchanged",
        key_before
        == (hashlib.sha256(key.read_bytes()).digest(), key.stat().st_uid, key.stat().st_mode),
    )
    check(
        "data_previous_container_removed",
        run("podman", "inspect", "shinyd-" + first, check=False).returncode != 0,
    )

    page.goto(app_url)
    page.get_by_role("link", name="Adicionar variável ou segredo", exact=True).click()
    page.get_by_label("Nome:", exact=True).fill("DATA_REVISION")
    page.get_by_label("Novo valor:", exact=True).fill("fixture-v2")
    page.get_by_role("button", name="Salvar", exact=True).click()
    page.get_by_role("link", name="Aplicar configuração", exact=True).click()
    activate("Aplicar configuração e reiniciar")
    third = open_app(app_url)
    expect_data(value)
    check("data_survives_configuration_restart", third != second)
    current = json.loads(run("podman", "inspect", "shinyd-" + third).stdout)[0]
    current_mapping = Path(f"/proc/{current['State']['Pid']}/uid_map").read_text()

    panel = urlsplit(app_url)._replace(path="/", query="", fragment="").geturl()
    page.goto(panel)
    page.get_by_role("link", name="+ Novo aplicativo", exact=True).click()
    page.get_by_label("Nome:", exact=True).fill("Outro app com dados")
    page.get_by_label("Endereço:", exact=True).fill("data-other")
    page.get_by_role("button", name="Salvar", exact=True).click()
    other_url = page.url
    other_id = str(uuid.UUID(urlsplit(other_url).path.strip("/").split("/")[-1]))
    data_apps.append(other_id)
    enable()
    publish(fixture)
    other = open_app(other_url)
    expect_data("data-empty")
    other_meta = json.loads(run("podman", "inspect", "shinyd-" + other).stdout)[0]
    other_mapping = Path(f"/proc/{other_meta['State']['Pid']}/uid_map").read_text()
    check("data_distinct_user_mappings", current_mapping != other_mapping)
    other_data = Path("/var/lib/shinyd-data") / other_id / "files"
    run(
        "podman",
        "exec",
        "shinyd-" + other,
        "Rscript",
        "--vanilla",
        "-e",
        'stopifnot(!file.exists("/data/escape"),!file.exists("/data/fixture.txt"),'
        '!dir.exists("/var/lib/shinyd-data"))',
    )
    page.get_by_label("Dado persistente", exact=True).fill("other-app-value")
    page.get_by_role("button", name="Gravar dado", exact=True).click()
    expect_data("other-app-value")
    check(
        "data_two_apps_isolated",
        (data / "fixture.txt").read_text().strip() == value
        and (other_data / "fixture.txt").read_text().strip() == "other-app-value",
    )
    from rollback_browser_probe import probe as rollback_probe

    third = rollback_probe(
        page, app_url, app_id, other_url, data, value, run, admin, check, open_app
    )
    from drain_browser_probe import probe as drain_probe

    third = drain_probe(page, app_url, other_url, data, value, run, check, open_app)
    from lifecycle_browser_probe import probe as lifecycle_probe

    third = lifecycle_probe(page, app_url, other_url, data, value, run, check, open_app)
    check(
        "data_container_removal_preserves_files",
        (other_data / "fixture.txt").read_text().strip() == "other-app-value",
    )
    check("data_other_app_preserved_after_removal", open_app(app_url) == third)
    expect_data(value)
    return urlsplit(page.url)._replace(path="", query="", fragment="").geturl(), {
        "filesystem": run("findmnt", "-n", "-o", "FSTYPE", "-T", str(data)).stdout.strip(),
        "crun": run("/usr/bin/crun", "--version").stdout.splitlines()[0],
        "uid_maps": [current_mapping.strip(), other_mapping.strip()],
    }

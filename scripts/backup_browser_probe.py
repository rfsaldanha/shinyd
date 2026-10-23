"""Cold recovery of the disposable installed fixture; never run on an occupied installation."""

import ast
import hashlib
import json
import secrets
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from installed_browser_probe import UNITS
from probe_progress import progress

BACKUP_ERRORS = {
    "Espaço insuficiente; mantenha ao menos 1 GiB livre além da operação.": "insufficient_space",
    "Imagens excedem o limite do backup.": "image_size_limit",
    "Exportação das imagens excedeu o tempo permitido.": "image_export_timeout",
    "Exportação das imagens falhou; confira Podman e espaço livre.": "image_export_failed",
    "Arquivo comprimido de imagens inválido.": "image_archive_invalid",
    "Arquivo comprimido de imagens incompleto.": "image_archive_incomplete",
    "Criptografia falhou: confira a chave, integridade e espaço livre.": "cryptography_failed",
    "Restaure somente em instalação vazia; configurações já existem.": "destination_occupied",
    "Restaure somente em instalação vazia; dados já existem.": "destination_occupied",
    "Arquivo final excede o limite do backup.": "archive_size_limit",
    "Comando de manutenção falhou; consulte os pré-requisitos do guia.": (
        "maintenance_command_failed"
    ),
    "Pare os serviços, sockets e timer do shinyd antes da manutenção.": "services_active",
    "Retire os containers do shinyd antes da manutenção.": "containers_present",
    "Links e arquivos especiais não são aceitos no backup.": "unsafe_entry",
    "Arquivo ausente, inacessível ou link inseguro.": "inaccessible_entry",
    "Backup/restauração não concluído. Confira o guia, arquivos e chaves; "
    "os serviços não foram iniciados.": "validation_or_io_failed",
}


def platform_errors():
    """Only literal errors in our own backup sources may enter a public report."""
    directory = Path(__file__).resolve().parents[1] / "backend/deployment"
    result = {}
    for path in sorted(directory.glob("backup*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Raise)
                and isinstance(node.exc, ast.Call)
                and isinstance(node.exc.func, ast.Name)
                and node.exc.func.id in {"BackupError", "SystemExit"}
                and len(node.exc.args) == 1
                and isinstance(node.exc.args[0], ast.Constant)
                and isinstance(node.exc.args[0].value, str)
            ):
                result.setdefault(node.exc.args[0].value, []).append(
                    {
                        "module": path.name,
                        "line": node.lineno,
                    }
                )
    return result


def failure_detail(result, operation):
    message = result.stderr.strip()
    detail = {
        "operation": operation,
        "exit_code": result.returncode,
        "reason": BACKUP_ERRORS.get(message, "unknown_failure"),
    }
    definitions = platform_errors().get(message)
    if definitions:
        if detail["reason"] == "unknown_failure":
            detail["reason"] = "platform_refusal"
        # A literal may be raised in several places; these are definitions, not a stack trace.
        detail["platform_error"] = {"definitions": definitions, "message": message}
    return detail


def backup_command(run, report, *args, timeout=1800):
    result = run(*args, timeout=timeout, check=False)
    if result.returncode:
        # Only literal platform errors cross this boundary. Never copy raw stderr.
        report["backup_failure"] = failure_detail(result, args[1])
        raise RuntimeError("backup_command_failed:" + report["backup_failure"]["reason"])
    return json.loads(result.stdout)


def refused_command(run, report, expected, *args, timeout=180):
    result = run(*args, timeout=timeout, check=False)
    detail = failure_detail(result, args[1])
    if result.returncode and detail["reason"] == expected:
        return True
    report["backup_failure"] = {**detail, "expected_reason": expected}
    return False


def prepare_environment(admin, app_id):
    """Create this test's own secret; an earlier scenario intentionally removed APP_TOKEN."""
    app_id = str(uuid.UUID(app_id))
    token = secrets.token_urlsafe(32)
    values = admin(
        "import json,hashlib\nfrom control.models import Application\n"
        "from control.services import decrypt_environment,set_environment\n"
        f"a=Application.objects.get(pk='{app_id}')\n"
        "assert a.desired_state=='stopped' and a.active_publication_id is None\n"
        f"set_environment(a.owner,a.pk,a.config_revision,'APP_TOKEN',{token!r},True)\n"
        "a.refresh_from_db()\n"
        "print('BACKUP='+json.dumps({'image':a.last_version.image_digest,"
        "'fingerprint':'|'.join(hashlib.sha256(decrypt_environment(a.environment.get(name=n))"
        ".encode()).hexdigest() for n in ['APP_TOKEN','APP_MESSAGE'])}))\n"
    )
    return json.loads(next(line[7:] for line in values.splitlines() if line.startswith("BACKUP=")))


def probe(page, app_url, password, run, admin, check, before_backup, report):
    progress("backup_start")
    panel = urlsplit(app_url)._replace(path="", query="", fragment="").geturl()
    app_id = app_url.rstrip("/").split("/")[-1]
    control = Path("/var/lib/shinyd-control")
    roots = [control, *[Path("/var/lib/shinyd-" + n) for n in ("data", "runtime", "builder")]]
    config = [
        Path(p)
        for p in (
            "/etc/shinyd/control.json",
            "/etc/shinyd-runtime/policy.json",
            "/etc/shinyd-builder/policy.json",
            "/etc/shinyd-gateway/key",
            "/etc/shinyd-tls/cert.pem",
            "/etc/shinyd-tls/key.pem",
            "/etc/shinyd-tls/Caddyfile",
        )
    ]
    units = [
        *UNITS,
        "shinyd-runtime.service",
        "shinyd-runtime.socket",
        "shinyd-builder.service",
        "shinyd-builder.socket",
    ]
    area = Path(tempfile.mkdtemp(prefix="shinyd-backup-probe-", dir="/var/lib"))
    complete = False
    try:
        key, wrong_key, output = area / "recovery.key", area / "wrong.key", area / "backup.age"
        run("age-keygen", "-o", str(key))
        run("age-keygen", "-o", str(wrong_key))
        public = run("age-keygen", "-y", str(key)).stdout.strip()
        create = ["shinyd-backup", "create", str(output), "--recipient", public]
        check(
            "backup_running_services_refused",
            refused_command(run, report, "services_active", *create),
        )
        page.goto(panel + "/server/backups/")
        page.get_by_role("heading", name="Backup e restauração", exact=True).wait_for()
        page.set_viewport_size({"width": 360, "height": 800})
        check(
            "backup_readiness_mobile",
            page.evaluate("document.documentElement.scrollWidth <= innerWidth"),
        )
        page.goto(panel + "/operations/")
        page.get_by_label("Operação:", exact=True).select_option("stop")
        page.get_by_label("Modo de parada ou reinício:", exact=True).select_option("immediate")
        page.get_by_label("Todos os aplicativos sob minha gestão neste momento").check()
        page.get_by_role("button", name="Revisar impacto", exact=True).click()
        page.get_by_label("Entendo o impacto sobre as conexões", exact=False).check()
        page.get_by_role("button", name="Confirmar operação", exact=True).click()
        page.get_by_text(
            "Processamento encerrado. Confira o resultado de cada aplicativo.", exact=True
        ).wait_for(timeout=240000)
        check(
            "backup_stop_all_through_browser",
            all(
                "Concluído" in text for text in page.locator(".operation-result").all_inner_texts()
            ),
        )
        before_backup()  # Validate and retire the separate executor/activation fixtures.
        data = Path("/var/lib/shinyd-data") / app_id / "files/fixture.txt"
        expected_data = data.read_bytes()
        expected_keys = {p.name: p.read_bytes() for p in (control / "keys").iterdir()}
        expected = prepare_environment(admin, app_id)
        check("backup_current_secret_prepared_after_removal_test")
        run("systemctl", "stop", *units)
        # The earlier isolation probe deliberately created this unsafe symlink.
        link = data.parent / "escape"
        check(
            "backup_unsafe_link_fixture_present",
            link.is_symlink()
            and str(link.readlink()) == "/var/lib/shinyd-control/keys/secrets.key",
        )
        check(
            "backup_unsafe_link_refused",
            refused_command(run, report, "inaccessible_entry", *create) and not output.exists(),
        )
        link.unlink()  # Only the exact disposable symlink checked above; never follow it.
        check(
            "backup_link_target_unchanged",
            (control / "keys/secrets.key").read_bytes() == expected_keys["secrets.key"],
        )
        progress("backup_create")
        created = backup_command(run, report, *create)
        check(
            "backup_encrypted_archive_created",
            output.stat().st_mode & 0o077 == 0
            and created["operation"] == "create"
            and created["images"] > 0,
        )
        with output.open("rb") as source:
            check("backup_age_format", source.read(22).startswith(b"age-encryption.org/v1"))
        check(
            "backup_wrong_identity_refused",
            refused_command(
                run,
                report,
                "cryptography_failed",
                "shinyd-backup",
                "verify",
                str(output),
                "--identity",
                str(wrong_key),
                timeout=180,
            ),
        )
        truncated = area / "truncated.age"
        with output.open("rb") as source:
            truncated.write_bytes(source.read(1024 * 1024))
        check(
            "backup_truncation_refused",
            refused_command(
                run,
                report,
                "cryptography_failed",
                "shinyd-backup",
                "verify",
                str(truncated),
                "--identity",
                str(key),
                timeout=180,
            ),
        )
        progress("backup_verify")
        verified = backup_command(
            run, report, "shinyd-backup", "verify", str(output), "--identity", str(key)
        )
        check("backup_full_verification", verified["backup_id"] == created["backup_id"])
        check(
            "backup_occupied_restore_refused",
            refused_command(
                run,
                report,
                "destination_occupied",
                "shinyd-backup",
                "restore",
                str(output),
                "--identity",
                str(key),
                timeout=180,
            )
            and data.read_bytes() == expected_data,
        )
        # Preserve the original fixture until the restored browser check passes.
        originals = area / "originals"
        originals.mkdir(mode=0o700)
        for index, path in enumerate([*roots, *config]):
            path.rename(originals / str(index))
        run(
            "systemd-tmpfiles",
            "--create",
            "/usr/lib/tmpfiles.d/shinyd-control.conf",
            "/usr/lib/tmpfiles.d/shinyd-runtime.conf",
        )
        for path in (roots[3], roots[3] / "artifacts", roots[3] / "jobs"):
            path.mkdir(mode=0o700, exist_ok=True)
        run("podman", "image", "rm", expected["image"], timeout=120)
        check(
            "backup_published_image_absent_before_restore",
            run("podman", "image", "exists", expected["image"], check=False).returncode != 0,
        )
        progress("backup_restore")
        restored = backup_command(
            run, report, "shinyd-backup", "restore", str(output), "--identity", str(key)
        )
        check("backup_empty_installation_restored", restored["backup_id"] == created["backup_id"])
        check(
            "backup_published_image_loaded",
            run("podman", "image", "exists", expected["image"], check=False).returncode == 0,
        )
        check(
            "backup_data_and_master_key_preserved",
            data.read_bytes() == expected_data
            and data.stat().st_uid == 65534
            and (control / "keys/secrets.key").read_bytes() == expected_keys["secrets.key"],
        )
        check(
            "backup_transport_keys_rotated",
            all(
                (control / "keys" / n).read_bytes() != expected_keys[n]
                for n in ("django.key", "gateway.key")
            ),
        )
        marker = Path("/var/lib/shinyd-backup/restore-incomplete")
        with marker.open("x") as stream:
            stream.write("disposable guard fixture\n")
        try:
            run("systemctl", "start", "shinyd.target", check=False)
            check(
                "backup_incomplete_restore_blocks_service_start",
                run("systemctl", "is-active", "shinyd-panel.service", check=False).returncode != 0,
            )
        finally:
            marker.unlink()
        progress("backup_browser")
        run("systemctl", "start", "shinyd.target")
        for _ in range(40):
            try:
                response = page.goto(panel + "/healthz", timeout=3000)
                if response.status == 200:
                    break
            except Exception:
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError("restored_panel_unavailable")
        page.goto(panel)
        check("backup_old_login_cookie_invalidated", "/login/" in page.url)
        page.get_by_label("Usuário:", exact=True).fill("installed-admin")
        page.get_by_label("Senha:", exact=True).fill(password)
        page.get_by_role("button", name="Entrar", exact=True).click()
        page.get_by_role("heading", name="Seus aplicativos").wait_for()
        check("backup_accounts_preserved")
        page.goto(panel + "/server/backups/")
        check("backup_restore_receipt_visible", created["backup_id"] in page.content())
        observed = admin(
            "from control.models import Application,RecoveryState,AppInstance\n"
            "print('SAFE='+str(not Application.objects.exclude(desired_state='stopped').exists() "
            "and not RecoveryState.objects.filter(enabled=True).exists() "
            "and not AppInstance.objects.filter(enabled=True).exists()))\n"
        )
        check("backup_apps_stopped_recovery_and_routes_disabled", "SAFE=True" in observed)
        page.goto(panel + "/operations/")
        page.get_by_label("Operação:", exact=True).select_option("start")
        page.locator(f'input[name="apps"][value="{app_id}"]').check()
        page.get_by_role("button", name="Revisar impacto", exact=True).click()
        page.get_by_label("Entendo o impacto sobre as conexões", exact=False).check()
        page.get_by_role("button", name="Confirmar operação", exact=True).click()
        page.get_by_text(
            "Processamento encerrado. Confira o resultado de cada aplicativo.", exact=True
        ).wait_for(timeout=240000)
        check(
            "backup_app_restart_completed",
            "Concluído" in page.locator(".operation-result").inner_text(),
        )
        page.goto(app_url)
        page.get_by_role("link", name="Abrir aplicativo", exact=True).click()
        page.get_by_role("button", name="Abrir aplicativo", exact=True).click()
        page.wait_for_function(
            "document.querySelector('#state')?.textContent.split('|').length === 5"
        )
        page.wait_for_function(
            "v=>document.querySelector('#data_state')?.textContent === v",
            arg=expected_data.decode().strip(),
        )
        page.wait_for_function(
            "v=>document.querySelector('#environment_fingerprint')?.textContent === v",
            arg=expected["fingerprint"],
        )
        before = page.locator("#state").inner_text().split("|")
        page.get_by_role("button", name="Somar", exact=True).click()
        page.wait_for_function(
            "v=>document.querySelector('#state')?.textContent === v",
            arg="|".join([*before[:4], str(int(before[4]) + 1)]),
        )
        check("backup_restored_shiny_reactive_with_data_and_secrets")
        with output.open("rb") as source:
            report["backup"] = {
                "id": created["backup_id"],
                "images": created["images"],
                "bytes": output.stat().st_size,
                "sha256": hashlib.file_digest(source, "sha256").hexdigest(),
            }
        complete = True
    finally:
        if complete or not ((area / "backup.age").exists() or (area / "originals").exists()):
            shutil.rmtree(area)
        else:
            # Failure is inspectable; never destroy the original fixture after a failed restore.
            report["backup_fixture_retained"] = str(area)

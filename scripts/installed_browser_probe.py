"""Installed HTTPS stack, real browser publication, disposable operator fixture only."""

import base64
import hashlib
import json
import os
import pwd
import secrets
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
import traceback
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from probe_cleanup import backup_interrupted
from probe_jobs import JobOutcomeError, wait_job
from probe_progress import progress
from probe_resources import snapshot as resource_snapshot

STATE = Path("/var/lib/shinyd-control")
CONFIG = Path("/etc/shinyd/control.json")
UNITS = [
    "shinyd.target",
    "shinyd-maintenance.timer",
    "shinyd-maintenance.service",
    *[f"shinyd-{name}.service" for name in ("ingress", "gateway", "worker", "auth", "panel")],
    *[f"shinyd-{name}.socket" for name in ("gateway", "auth", "panel")],
]


def run(*args, input=None, timeout=120, check=True):
    result = subprocess.run(args, input=input, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError("installed_command_failed:" + Path(args[0]).name)
    return result


def certificate(directory):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "shinyd disposable TLS fixture")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=5))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("panel.shinyd.test"), x509.DNSName("*.apps.shinyd.test")]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    (directory / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (directory / "key.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    spki = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return base64.b64encode(hashlib.sha256(spki).digest()).decode()


def require_empty_control(state, configuration_paths):
    if state.is_symlink() or (state.exists() and (not state.is_dir() or any(state.iterdir()))):
        raise RuntimeError("installed_control_not_empty")
    if any(path.exists() or path.is_symlink() for path in configuration_paths):
        raise RuntimeError("installed_control_not_empty")


def installed_probe(package, fixture, created, data_apps, before_backup=None):
    from playwright.sync_api import sync_playwright

    progress("installed_start")
    report = {
        "passed": False,
        "checks": [],
        "limitations": [
            "Certificado de teste fixado por SPKI no Chrome; "
            "não testa emissão ACME ou DNS público.",
            "Serviços instalados e reiniciados; não reinicia a máquina nem mede capacidade.",
        ],
    }
    stage, owned = "preflight", False
    start = datetime.now(UTC).isoformat()
    password = secrets.token_urlsafe(32)
    app_token = secrets.token_urlsafe(32)
    rotated_token = secrets.token_urlsafe(32)
    app_message = "fixture-" + secrets.token_hex(16) + "\nç='quoted'\\value\r\nsecond line"

    def check(name, value=True):
        if not value:
            report["failed_check"] = name
            raise AssertionError(name)
        report["checks"].append(name)

    def admin(code):
        return run("/usr/bin/shinyd-admin", "shell", "--interface", "python", input=code).stdout

    try:
        require_empty_control(
            STATE,
            [
                CONFIG,
                Path("/etc/shinyd-gateway/key"),
                *[Path("/etc/shinyd-tls") / name for name in ("cert.pem", "key.pem", "Caddyfile")],
            ],
        )
        check("installed_empty_control")
        package = package.resolve(strict=True)
        checksum = hashlib.sha256(package.read_bytes()).hexdigest()
        check(
            "control_package_checksum",
            checksum == package.with_suffix(".deb.sha256").read_text().split()[0],
        )
        report["package_sha256"] = checksum
        check(
            "control_package_identity",
            run("dpkg-deb", "--show", "--showformat=${Package} ${Version}", str(package)).stdout
            == "shinyd-control 0.1.0~control10",
        )
        stage = "installation"
        run("dpkg", "--install", str(package))
        owned = True
        run("systemd-analyze", "verify", "--man=no", *UNITS)
        check("installed_units_verified")
        port = 18443
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", port))
        with tempfile.TemporaryDirectory(prefix="shinyd-installed-probe-") as temporary:
            temporary = Path(temporary)
            pin = certificate(temporary)
            stage = "configuration"
            configure = [
                "/usr/bin/shinyd-configure",
                "--panel-host",
                "panel.shinyd.test",
                "--app-domain",
                "apps.shinyd.test",
                "--https-port",
                str(port),
                "--bind",
                "127.0.0.1",
                "--certificate",
                str(temporary / "cert.pem"),
                "--private-key",
                str(temporary / "key.pem"),
            ]
            run(*configure)
            keys = {p.name: p.read_bytes() for p in (STATE / "keys").iterdir()}
            run(*configure)
            check(
                "configuration_preserves_keys",
                keys == {p.name: p.read_bytes() for p in (STATE / "keys").iterdir()},
            )
            check("configuration_private_backup", any((STATE / "backups").glob("*.sqlite3")))
            admin(
                "from control.models import User\n"
                'User.objects.create_superuser("installed-admin", password='
                + repr(password)
                + ")\n"
            )
            stage = "services"
            run("systemctl", "start", "shinyd.target")
            for name in ("panel", "auth", "worker", "gateway", "ingress"):
                check(
                    "installed_service_" + name,
                    run("systemctl", "is-active", "shinyd-" + name + ".service").stdout.strip()
                    == "active",
                )
            check(
                "configuration_refuses_running_services",
                run(*configure, check=False).returncode != 0,
            )
            for name in ("panel", "auth", "gateway"):
                path = Path("/run/shinyd-" + name + "/http.sock")
                check(
                    "private_socket_" + name, path.is_socket() and path.stat().st_mode & 0o007 == 0
                )
            outsider = pwd.getpwuid(int(os.environ["SUDO_UID"])).pw_name
            denied = run(
                "runuser",
                "-u",
                outsider,
                "--",
                "python3",
                "-c",
                "import socket; s=socket.socket(socket.AF_UNIX); "
                "s.connect('/run/shinyd-panel/http.sock')",
                check=False,
            )
            check("unprivileged_panel_socket_denied", denied.returncode != 0)
            stage = "tls"
            context = ssl.create_default_context(cafile=str(temporary / "cert.pem"))
            for _ in range(60):
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=2) as tcp:
                        with context.wrap_socket(tcp, server_hostname="panel.shinyd.test") as tls:
                            check(
                                "tls_hostname_and_certificate",
                                tls.version() in ("TLSv1.2", "TLSv1.3"),
                            )
                    break
                except (ConnectionError, TimeoutError):
                    time.sleep(0.5)
            else:
                raise RuntimeError("tls_unavailable")
            panel = f"https://panel.shinyd.test:{port}"
            stage = "browser_login"
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    executable_path="/usr/bin/google-chrome",
                    headless=True,
                    args=[
                        "--no-proxy-server",
                        "--host-resolver-rules=MAP *.shinyd.test 127.0.0.1",
                        "--ignore-certificate-errors-spki-list=" + pin,
                    ],
                )
                session = browser.new_context(accept_downloads=True)
                page = session.new_page()
                page.set_default_timeout(20000)
                errors = []
                page.on("pageerror", lambda error: errors.append(type(error).__name__))
                page.goto(panel)
                page.get_by_label("Usuário:", exact=True).fill("installed-admin")
                page.get_by_label("Senha:", exact=True).fill(password)
                page.get_by_role("button", name="Entrar", exact=True).click()
                page.get_by_role("heading", name="Seus aplicativos").wait_for()
                check(
                    "https_login_and_static_assets",
                    page.evaluate("getComputedStyle(document.body).fontFamily")
                    != '"Times New Roman"',
                )
                check(
                    "internal_api_not_exposed",
                    page.evaluate(
                        "async () => (await fetch('/v2/request', {method:'POST'})).status"
                    )
                    in (403, 404),
                )
                stage = "browser_publication"
                page.get_by_role("link", name="+ Novo aplicativo", exact=True).click()
                page.get_by_label("Nome:", exact=True).fill("Aplicativo HTTPS de teste")
                page.get_by_label("Endereço:", exact=True).fill("installed-fixture")
                page.locator("#id_visibility").select_option("private")
                page.get_by_role("button", name="Salvar", exact=True).click()
                app_url = page.url
                page.get_by_role("link", name="Adicionar variável ou segredo", exact=True).click()
                page.get_by_label("Nome:", exact=True).fill("APP_TOKEN")
                page.get_by_label("Novo valor:", exact=True).fill(app_token)
                page.get_by_role("button", name="Salvar", exact=True).click()
                check("secret_not_rendered_after_save", app_token not in page.content())
                # Exercise multiline UTF-8 through the same service, supplied on stdin.
                admin(
                    "from control.models import Application\n"
                    "from control.services import set_environment\n"
                    "a=Application.objects.get(slug='installed-fixture')\n"
                    f"set_environment(a.owner,a.pk,a.config_revision,'APP_MESSAGE',{app_message!r},True)\n"
                )
                page.get_by_role("link", name="Enviar ZIP", exact=True).click()
                page.get_by_label("Aplicativo em ZIP:", exact=True).set_input_files(str(fixture))
                page.get_by_role("button", name="Salvar", exact=True).click()
                wait_job(page, timeout=90000)
                check("browser_upload_and_inspection")
                page.get_by_role("link", name="Preparar versão", exact=True).click()
                page.get_by_label("Ambiente R aprovado:", exact=True).select_option("r-a")
                page.get_by_role("button", name="Salvar", exact=True).click()
                wait_job(page, timeout=180000)
                check("browser_prepare_real_image")
                page.get_by_role("link", name="Ativar versão", exact=True).click()
                page.get_by_label("Entendo o impacto do modo de troca", exact=False).check()
                page.get_by_role("button", name="Ativar versão", exact=True).click()
                wait_job(page, timeout=180000)
                check("browser_activate_real_instance")
                page.get_by_role("link", name="Voltar ao app", exact=True).click()
                page.get_by_role("link", name="Abrir aplicativo", exact=True).click()
                page.get_by_role("button", name="Abrir aplicativo", exact=True).click()
                page.wait_for_function(
                    "document.querySelector('#state')?.textContent.split('|').length === 5"
                )
                app_origin = urlsplit(page.url)._replace(path="", query="", fragment="").geturl()
                before = page.locator("#state").inner_text().split("|")

                def fingerprint(token):
                    return (
                        hashlib.sha256(token.encode()).hexdigest()
                        + "|"
                        + hashlib.sha256(app_message.encode()).hexdigest()
                    )

                page.wait_for_function(
                    "value => document.querySelector('#environment_fingerprint')"
                    "?.textContent === value",
                    arg=fingerprint(app_token),
                )
                check("r_receives_secret_and_multiline_utf8")
                existing_pids = {}
                for identity in created:
                    inspected = run("podman", "inspect", "shinyd-" + identity, check=False)
                    if inspected.returncode:
                        continue  # A fixture was deliberately removed to free a slot.
                    metadata = json.loads(inspected.stdout)[0]
                    if metadata["State"]["Running"]:
                        existing_pids[identity] = metadata["State"]["Pid"]
                check("other_app_present_for_configuration_test", bool(existing_pids))
                check(
                    "production_cookie_boundary",
                    page.locator("#credential_boundary").inner_text() == "credential-boundary-ok",
                )
                cookies = session.cookies()
                for name, host in [
                    ("__Host-shinyd_session", "panel.shinyd.test"),
                    ("__Host-shinyd_app", urlsplit(app_origin).hostname),
                ]:
                    cookie = next(c for c in cookies if c["name"] == name)
                    check(
                        "secure_cookie_" + name,
                        cookie["domain"] == host
                        and cookie["secure"]
                        and cookie["httpOnly"]
                        and cookie["sameSite"] == "Lax",
                    )
                stage = "browser_shiny"
                page.get_by_role("button", name="Somar", exact=True).click()
                page.wait_for_function(
                    "document.querySelector('#state').textContent.endsWith('|1')"
                )
                page.locator("input[type=file]").set_input_files(
                    {
                        "name": "fixture.txt",
                        "mimeType": "text/plain",
                        "buffer": b"installed-https-ok",
                    }
                )
                page.wait_for_function(
                    "document.querySelector('#uploaded').textContent === 'installed-https-ok'"
                )
                with page.expect_download() as download:
                    page.get_by_role("link", name="Baixar estado").click()
                fields = Path(download.value.path()).read_text().strip().split("|")
                check(
                    "https_shiny_affinity_upload_download",
                    fields[:4] == before[:4] and fields[-1] == "1",
                )
                anonymous = browser.new_context()
                denial = anonymous.new_page()
                check("https_private_direct_url_denied", denial.goto(app_origin).status == 403)
                outcome = denial.evaluate(
                    """async address => new Promise(resolve => {
                    const ws=new WebSocket(address+'/websocket/');
                    ws.onopen=()=>{ws.close();resolve('opened')};
                    ws.onerror=()=>resolve('denied');
                    setTimeout(()=>{ws.close();resolve('timeout')},5000);
                })""",
                    app_origin.replace("https:", "wss:"),
                )
                check("https_private_websocket_denied", outcome == "denied")
                stage = "service_restart"

                def data_service_pids():
                    return {
                        name: run(
                            "systemctl",
                            "show",
                            f"shinyd-{name}.service",
                            "--property=MainPID",
                            "--value",
                        ).stdout.strip()
                        for name in ("auth", "gateway", "ingress")
                    }

                data_pids = data_service_pids()
                run("systemctl", "restart", "shinyd-panel.service", "shinyd-worker.service")
                after_pids = data_service_pids()
                report["service_restart"] = {"before": data_pids, "after": after_pids}
                check(
                    "panel_restart_does_not_restart_data_services",
                    data_pids == after_pids and "0" not in after_pids.values(),
                )
                page.get_by_role("button", name="Somar", exact=True).click()
                page.wait_for_function(
                    "document.querySelector('#state').textContent.endsWith('|2')"
                )
                check(
                    "panel_worker_restart_preserves_shiny",
                    page.locator("#state").inner_text().split("|")[:4] == before[:4],
                )
                stage = "browser_environment_rotation"
                panel_page = session.new_page()
                panel_page.goto(app_url)
                panel_page.get_by_role(
                    "link", name="Adicionar variável ou segredo", exact=True
                ).click()
                panel_page.get_by_label("Nome:", exact=True).fill("APP_TOKEN")
                panel_page.get_by_label("Novo valor:", exact=True).fill(rotated_token)
                panel_page.get_by_role("button", name="Salvar", exact=True).click()
                check(
                    "saved_secret_waits_for_explicit_apply",
                    page.locator("#environment_fingerprint").inner_text() == fingerprint(app_token),
                )

                def apply_and_open():
                    panel_page.get_by_role("link", name="Aplicar configuração", exact=True).click()
                    panel_page.get_by_label(
                        "Entendo o impacto do modo de troca", exact=False
                    ).check()
                    panel_page.get_by_role(
                        "button", name="Aplicar configuração e reiniciar", exact=True
                    ).click()
                    wait_job(panel_page, timeout=180000)
                    panel_page.get_by_role("link", name="Voltar ao app", exact=True).click()
                    panel_page.get_by_role("link", name="Abrir aplicativo", exact=True).click()
                    panel_page.get_by_role("button", name="Abrir aplicativo", exact=True).click()
                    panel_page.wait_for_function(
                        "document.querySelector('#state')?.textContent.split('|').length === 5"
                    )
                    return (
                        urlsplit(panel_page.url)._replace(path="", query="", fragment="").geturl()
                    )

                previous_origin = app_origin
                app_origin = apply_and_open()
                panel_page.wait_for_function(
                    "value => document.querySelector('#environment_fingerprint')"
                    "?.textContent === value",
                    arg=fingerprint(rotated_token),
                )
                check("secret_rotated_without_zip", app_origin != previous_origin)
                report["previous_config_http_status"] = page.goto(previous_origin).status
                # Disabled publications are removed routes; existing private
                # routes without permission return 403 instead.
                check("previous_config_route_revoked", report["previous_config_http_status"] == 404)
                check(
                    "single_source_version_after_rotation",
                    "VERSION_COUNT=1"
                    in admin(
                        "from control.models import Version\n"
                        "print('VERSION_COUNT='+str(Version.objects.count()))\n"
                    ),
                )

                active_id = panel_page.locator("#state").inner_text().split("|")[0]
                metadata = run("podman", "inspect", "shinyd-" + active_id).stdout
                container_log = run("podman", "logs", "shinyd-" + active_id)
                sensitive_values = [app_token, rotated_token, app_message]
                check(
                    "secrets_absent_from_container_metadata_and_logs",
                    all(
                        value not in metadata + container_log.stdout + container_log.stderr
                        for value in sensitive_values
                    ),
                )
                check(
                    "secrets_absent_from_sqlite_plaintext",
                    all(
                        value.encode() not in path.read_bytes()
                        for path in STATE.glob("metadata.sqlite3*")
                        if path.is_file()
                        for value in sensitive_values
                    ),
                )
                check(
                    "other_apps_keep_processes_during_secret_rotation",
                    all(
                        json.loads(run("podman", "inspect", "shinyd-" + identity).stdout)[0][
                            "State"
                        ]["Pid"]
                        == pid
                        for identity, pid in existing_pids.items()
                    ),
                )

                panel_page.goto(app_url)
                panel_page.locator(".data-list li").filter(has_text="APP_TOKEN").get_by_role(
                    "link", name="Remover", exact=True
                ).click()
                panel_page.get_by_label("Confirmo a remoção desta variável.", exact=True).check()
                panel_page.get_by_role("button", name="Remover variável", exact=True).click()
                app_origin = apply_and_open()
                panel_page.wait_for_function(
                    "document.querySelector('#environment_fingerprint')"
                    "?.textContent === 'environment-unset'"
                )
                check("removed_secret_unset_after_apply")
                stage = "persistent_data"
                progress("persistent_data")
                from persistent_browser_probe import probe as data_probe

                app_origin, report["persistent_data"] = data_probe(
                    panel_page,
                    app_url,
                    fixture,
                    temporary,
                    run,
                    admin,
                    check,
                    created,
                    data_apps,
                    report,
                )
                check(
                    "other_apps_keep_processes_during_data_publications",
                    all(
                        json.loads(run("podman", "inspect", "shinyd-" + identity).stdout)[0][
                            "State"
                        ]["Pid"]
                        == pid
                        for identity, pid in existing_pids.items()
                    ),
                )
                stage = "browser_public"
                panel_page.goto(app_url)
                panel_page.get_by_role("link", name="Configurar app", exact=True).click()
                panel_page.locator("#id_visibility").select_option("public")
                panel_page.get_by_role("button", name="Salvar", exact=True).click()
                check("https_public_direct_url", denial.goto(app_origin).status == 200)
                denial.wait_for_function(
                    "document.querySelector('#state')?.textContent.split('|').length === 5"
                )
                check("https_public_websocket")
                panel_page.set_viewport_size({"width": 360, "height": 800})
                check(
                    "installed_panel_mobile",
                    panel_page.evaluate("document.documentElement.scrollWidth <= innerWidth"),
                )
                check("browser_no_javascript_errors", not errors)
                sensitive = [
                    password,
                    app_token,
                    rotated_token,
                    app_message,
                    *[c["value"] for c in cookies],
                    *[v.decode() for k, v in keys.items() if k != "secrets.key"],
                ]
                log = run(
                    "journalctl",
                    "--since",
                    start,
                    "--no-pager",
                    "-o",
                    "cat",
                    *[
                        arg
                        for name in ("panel", "auth", "worker", "gateway", "ingress", "runtime")
                        for arg in ("-u", "shinyd-" + name + ".service")
                    ],
                ).stdout
                check(
                    "credentials_absent_from_service_logs",
                    all(value not in log for value in sensitive),
                )
                if before_backup is not None:
                    from backup_browser_probe import probe as backup_probe

                    stage = "backup_restore"
                    backup_probe(
                        panel_page, app_url, password, run, admin, check, before_backup, report
                    )
                browser.close()
        report["passed"] = True
    except Exception as exc:
        report["resources_at_failure"] = resource_snapshot()
        if isinstance(exc, JobOutcomeError):
            report["job_failure"] = {"expected": exc.expected, "observed": exc.observed}
        report["error"] = {
            "stage": stage,
            "type": type(exc).__name__,
            "frames": [
                {"file": Path(f.filename).name, "line": f.lineno}
                for f in traceback.extract_tb(exc.__traceback__)
            ],
        }
        if stage == "persistent_data":
            # Only platform-owned status fields, never app logs or environment values.
            try:
                diagnostic = admin(
                    "import json\nfrom control.models import Job\n"
                    "print('DATA_JOBS='+json.dumps(list(Job.objects.order_by('-created_at')"
                    "[:6].values('kind','status','phase','message'))))\n"
                )
                report["data_jobs"] = json.loads(
                    next(
                        line.removeprefix("DATA_JOBS=")
                        for line in diagnostic.splitlines()
                        if line.startswith("DATA_JOBS=")
                    )
                )
            except Exception:
                report["data_jobs_unavailable"] = True
        report["services"] = run(
            "systemctl",
            "show",
            *UNITS,
            "--property=Id,ActiveState,Result,ExecMainStatus",
            check=False,
        ).stdout.splitlines()
    finally:
        if owned:
            stopped = run("systemctl", "stop", *UNITS, check=False)
            if backup_interrupted():
                report["cleanup_error"] = "backup_interrupted_preserved"
            elif stopped.returncode:
                report["cleanup_error"] = "installed_services_stop"
            elif CONFIG.exists() and (STATE / "metadata.sqlite3").exists():
                try:
                    data = admin(
                        "import json\nfrom control.models import AppInstance, Publication\n"
                        "from control.runtime_client import probe_id\n"
                        "ids=list(AppInstance.objects.values_list('id',flat=True))\n"
                        "ids.extend(probe_id(p) for p in Publication.objects.all() "
                        "if p.runtime_spec.get('data_mode')=='persistent')\n"
                        "print('INSTANCE_IDS='+json.dumps(ids,default=str))\n"
                    )
                    identities = json.loads(
                        next(
                            line.removeprefix("INSTANCE_IDS=")
                            for line in data.splitlines()
                            if line.startswith("INSTANCE_IDS=")
                        )
                    )
                    created.extend(identities)
                    check("installed_cleanup_instances_recorded")
                except Exception:
                    report["cleanup_error"] = "installed_instances_inventory"
            if not report.get("cleanup_error"):
                for path in (
                    CONFIG,
                    Path("/etc/shinyd-gateway/key"),
                    *[Path("/etc/shinyd-tls") / n for n in ("cert.pem", "key.pem", "Caddyfile")],
                ):
                    path.unlink(missing_ok=True)
                shutil.rmtree(STATE)
                run("systemd-tmpfiles", "--create", "/usr/lib/tmpfiles.d/shinyd-control.conf")
    report["passed"] = report["passed"] and not report.get("cleanup_error")
    return report

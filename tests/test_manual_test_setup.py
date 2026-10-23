import copy
import json
import os
import ssl
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import manual_test_common as common
import manual_test_setup as setup
import open_manual_test as browser
import pytest

from control.archives import inspect_archive
from deployment.cli import verify_certificate


@pytest.fixture
def report():
    # The integration evidence is immutable test input with observed package versions.
    return json.loads(
        (common.ROOT / "docs/evidencias/backup-instalado-local-sucesso.json").read_text()
    )


@pytest.fixture
def plan(report):
    return common.plan_from_report(json.dumps(report).encode())


@pytest.mark.parametrize(
    "problem", ["failed", "cleanup", "missing_check", "image", "packages", "shape"]
)
def test_plan_requires_successful_backup_and_bounded_observed_images(report, problem):
    if problem == "failed":
        report["passed"] = False
    elif problem == "cleanup":
        report["cleanup_error"] = ["failed"]
    elif problem == "missing_check":
        report["tests"] = [
            t for t in report["tests"] if t["name"] != "backup_empty_installation_restored"
        ]
    elif problem == "image":
        report["artifacts"][0]["base_image"] = "docker.io/unapproved:latest"
    elif problem == "packages":
        report["artifacts"][0]["packages"]["shiny"] = "1.0; system('private')"
    else:
        report = []
    with pytest.raises(ValueError):
        common.plan_from_report(json.dumps(report).encode())


@pytest.mark.parametrize("environment", ["r-a", "r-b"])
@pytest.mark.parametrize("revision", ["v1", "v2"])
def test_examples_are_deterministic_and_accepted_by_zip_and_lock_validation(
    tmp_path, plan, environment, revision
):
    from builder.policy import resolve_lock

    observed = plan["builder"]["environments"][environment]
    data = common.sample_zip(observed, revision)
    assert common.sample_zip(observed, revision) == data
    source = tmp_path / "app.zip"
    source.write_bytes(data)
    extracted = tmp_path / "out"
    manifest = inspect_archive(source, extracted, max_bytes=1000000, max_entries=10)
    assert manifest["r_version"] == observed["r_version"]
    assert manifest["has_lockfile"]
    lock = resolve_lock(extracted / "renv.lock", observed)
    assert {k: v["Version"] for k, v in lock["Packages"].items()} == observed["packages"]
    assert f'REVISAO <- "{revision}"' in (extracted / "app.R").read_text()


@pytest.fixture
def host(tmp_path):
    for name in (
        "etc/shinyd",
        "etc/shinyd-builder",
        "etc/shinyd-runtime",
        "etc/shinyd-tls",
        "etc/shinyd-gateway",
        "var/lib/shinyd-control",
        "var/lib/shinyd-builder/jobs",
        "var/lib/shinyd-builder/artifacts",
        "var/lib/shinyd-data",
        "var/lib/shinyd-runtime",
        "var/lib/shinyd-backup",
        "usr/local/share",
    ):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
    for path in tmp_path.rglob("*"):
        if path.is_dir():
            path.chmod(0o755)
    return tmp_path


@pytest.fixture
def commands(host):
    calls = []

    def run(*args):
        calls.append(args)
        if args[0] == "dpkg-query":
            return "installed " + common.PACKAGES[args[-1]]
        if args[:2] == ("systemctl", "show"):
            return "inactive\ninactive\n"
        if args[:2] == ("podman", "ps"):
            return "[]"
        if args[:3] == ("podman", "image", "inspect"):
            return json.dumps([{"Id": args[-1], "Config": {}}])
        if args[0] == "shinyd-configure":
            private = host / str(common.PRIVATE).lstrip("/")
            pem, key = (private / "cert.pem").read_bytes(), (private / "key.pem").read_bytes()
            verify_certificate(pem, key, common.CONFIG)
            target = host / "etc/shinyd/control.json"
            target.write_text(json.dumps(common.CONFIG))
            target.chmod(0o640)
            cert = host / "etc/shinyd-tls/cert.pem"
            cert.write_bytes(pem)
            cert.chmod(0o640)
            marker = host / "var/lib/shinyd-control/fixture-key"
            if not marker.exists():
                marker.write_bytes(b"PRIVATE_TEST_SENTINEL")
            return ""
        if args[:2] == ("systemctl", "set-property"):
            return ""
        pytest.fail("Unexpected command")

    return run, calls


def test_setup_repetition_keeps_keys_and_does_not_restart_or_reconfigure(host, plan, commands):
    run, calls = commands
    first = setup.prepare(plan, root=host, run=run, owner=os.getuid())
    marker = host / "var/lib/shinyd-control/fixture-key"
    before = marker.stat().st_mtime_ns
    calls.clear()
    assert setup.prepare(plan, root=host, run=run, owner=os.getuid()) == first
    assert marker.read_bytes() == b"PRIVATE_TEST_SENTINEL" and marker.stat().st_mtime_ns == before
    assert all(args[0] == "dpkg-query" for args in calls)
    public = host / str(common.PUBLIC).lstrip("/")
    value, pem = browser.profile_settings(public, owner=os.getuid())
    assert value["origin"] == common.ORIGIN and pem == (public / "cert.pem").read_bytes()
    assert all((public / name).is_file() for name in value["sample_zips"])
    assert "PRIVATE_TEST_SENTINEL" not in json.dumps(value)
    assert not list(public.glob("*key*"))


def test_retry_after_configuration_failure_preserves_generated_certificate_and_existing_keys(
    host, plan, commands
):
    run, calls = commands
    failed = False

    def interrupted(*args):
        nonlocal failed
        result = run(*args)
        if args[0] == "shinyd-configure" and not failed:
            failed = True
            raise RuntimeError("setup_command_failed:shinyd-configure")
        return result

    with pytest.raises(RuntimeError):
        setup.prepare(plan, root=host, run=interrupted, owner=os.getuid())
    private = host / str(common.PRIVATE).lstrip("/")
    cert, key = (private / "cert.pem").read_bytes(), (private / "key.pem").read_bytes()
    result = setup.prepare(plan, root=host, run=interrupted, owner=os.getuid())
    assert result["configured"]
    assert (private / "cert.pem").read_bytes() == cert and (private / "key.pem").read_bytes() == key
    assert (host / "var/lib/shinyd-control/fixture-key").read_bytes() == b"PRIVATE_TEST_SENTINEL"


@pytest.mark.parametrize(
    "path",
    [
        "var/lib/shinyd-control/existing",
        "etc/shinyd-runtime/policy.json",
        "var/lib/shinyd-data/existing",
    ],
)
def test_setup_never_replaces_an_occupied_installation(host, plan, commands, path):
    occupied = host / path
    occupied.write_bytes(b"PRIVATE_EXISTING_DATA")
    run, calls = commands
    with pytest.raises((ValueError, RuntimeError)):
        setup.prepare(plan, root=host, run=run, owner=os.getuid())
    assert occupied.read_bytes() == b"PRIVATE_EXISTING_DATA"
    assert not (host / str(common.PRIVATE).lstrip("/")).exists()
    assert all(args[0] != "shinyd-configure" for args in calls)


def test_setup_rejects_different_report_after_partial_preparation(host, plan, commands):
    run, calls = commands
    setup.prepare(plan, root=host, run=run, owner=os.getuid())
    changed = copy.deepcopy(plan)
    changed["source_report_sha256"] = "b" * 64
    with pytest.raises(ValueError, match="different_setup_source"):
        setup.prepare(changed, root=host, run=run, owner=os.getuid())


@pytest.mark.parametrize(
    "field,value",
    [("origin", "https://untrusted.test"), ("spki", "invalid"), ("certificate_sha256", "0" * 64)],
)
def test_launcher_rejects_modified_certificate_pin_and_origin(host, plan, commands, field, value):
    run, _ = commands
    setup.prepare(plan, root=host, run=run, owner=os.getuid())
    public = host / str(common.PUBLIC).lstrip("/")
    path = public / "setup.json"
    changed = json.loads(path.read_text())
    changed[field] = value
    path.write_text(json.dumps(changed))
    with pytest.raises(ValueError):
        browser.profile_settings(public, owner=os.getuid())


def test_file_reader_refuses_links_and_group_writes(tmp_path):
    target = tmp_path / "report.json"
    target.write_bytes(b"{}")
    target.chmod(0o664)
    with pytest.raises(ValueError, match="unsafe_file"):
        common.read_file(target, owners={os.getuid()})
    target.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(OSError):
        common.read_file(link, owners={os.getuid()})


def test_browser_flags_use_separate_profile_and_one_key_pin(plan, tmp_path):
    pem, _ = common.certificate_pair()
    value = common.public_manifest(plan, pem)
    args = browser.browser_arguments(value, tmp_path)
    assert "--ignore-certificate-errors" not in args
    assert "--user-data-dir=" + str(tmp_path) in args
    assert args.count("--ignore-certificate-errors-spki-list=" + value["spki"]) == 1
    assert args[-1] == common.ORIGIN


def test_health_uses_hostname_and_certificate_validation(tmp_path, monkeypatch):
    pem, key = common.certificate_pair()
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(pem)
    key_path.write_bytes(key)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200 if self.path == "/healthz" else 404)
            self.end_headers()

    server = HTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setattr(browser, "CONFIG", {**common.CONFIG, "https_port": server.server_port})
        browser.health(pem)
        other, _ = common.certificate_pair()
        with pytest.raises(ssl.SSLCertVerificationError):
            browser.health(other)
        monkeypatch.setattr(
            browser, "CONFIG", {**browser.CONFIG, "panel_host": "wrong.shinyd.test"}
        )
        with pytest.raises(ssl.SSLCertVerificationError):
            browser.health(pem)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_launcher_waits_for_startup_but_does_not_retry_bad_tls(monkeypatch):
    calls, sleeps = [], []

    def health(pem):
        calls.append(pem)
        if len(calls) == 1:
            raise ConnectionRefusedError
        if len(calls) == 2:
            raise ValueError("panel_health_failed")

    monkeypatch.setattr(browser, "health", health)
    monkeypatch.setattr(browser.time, "sleep", sleeps.append)
    browser.wait_health(b"fixture")
    assert len(calls) == 3 and sleeps == [0.25, 0.25]

    def bad_certificate(pem):
        raise ssl.SSLCertVerificationError

    monkeypatch.setattr(browser, "health", bad_certificate)
    with pytest.raises(ssl.SSLCertVerificationError):
        browser.wait_health(b"fixture")
    assert sleeps == [0.25, 0.25]


def test_startup_wait_is_bounded(monkeypatch):
    def unavailable(pem):
        raise ConnectionRefusedError

    times = iter([0, 1])
    monkeypatch.setattr(browser, "health", unavailable)
    monkeypatch.setattr(browser.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(browser.time, "sleep", lambda n: pytest.fail("deadline already expired"))
    with pytest.raises(TimeoutError, match="panel_startup_timeout"):
        browser.wait_health(b"fixture", timeout=1)

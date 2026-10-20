from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from deployment.cli import verify_certificate, write_private
from deployment.config import environment, validate

CONFIG = dict(
    schema_version=1,
    panel_host="panel.example.org",
    app_domain="apps.example.org",
    https_port=443,
    bind="0.0.0.0",
)


@pytest.mark.parametrize(
    "change",
    [
        {"panel_host": "panel.example.org\nadmin on"},
        {"app_domain": "*.example.org"},
        {"panel_host": "panel.apps.example.org"},
        {"app_domain": "apps.panel.example.org"},
        {"panel_host": "apps.example.org"},
        {"panel_host": "127.0.0.1"},
        {"app_domain": "apps.example.org:443"},
        {"https_port": True},
        {"https_port": 0},
        {"https_port": 65536},
        {"bind": "localhost"},
        {"extra": "ignored"},
        {"schema_version": True},
    ],
)
def test_rejects_ambiguous_or_unsafe_installation_configuration(change):
    with pytest.raises(ValueError):
        validate({**CONFIG, **change})


def test_installed_environment_cannot_enable_development_or_secret_override(monkeypatch):
    monkeypatch.setenv("SHINYD_DEV", "1")
    monkeypatch.setenv("SHINYD_SECRET_KEY", "inherited-untrusted-value")
    value = environment({**CONFIG, "https_port": 18443})
    assert "SHINYD_DEV" not in value and "SHINYD_SECRET_KEY" not in value
    assert value["SHINYD_PANEL_ORIGIN"] == "https://panel.example.org:18443"
    assert value["SHINYD_HTTPS_PORT"] == "18443"


def pem(names, expired=False):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(days=2))
        .not_valid_after(datetime.now(UTC) + timedelta(days=-1 if expired else 1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in names]), False)
        .sign(key, hashes.SHA256())
    )
    return (
        cert.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )


def test_certificate_covers_exact_panel_and_wildcard_apps():
    cert, key = pem(["panel.example.org", "*.apps.example.org"])
    verify_certificate(cert, key, CONFIG)
    _, other = pem(["panel.example.org", "*.apps.example.org"])
    with pytest.raises(ValueError, match="não correspondem"):
        verify_certificate(cert, other, CONFIG)


@pytest.mark.parametrize(
    "names,expired",
    [
        (["panel.example.org", "*.example.org"], False),
        (["*.apps.example.org"], False),
        (["panel.example.org", "*.apps.example.org"], True),
    ],
)
def test_rejects_tls_certificate_that_cannot_serve_configured_hosts(names, expired):
    cert, key = pem(names, expired)
    with pytest.raises(ValueError):
        verify_certificate(cert, key, CONFIG)


def test_configuration_writer_refuses_service_writable_directory(tmp_path):
    tmp_path.chmod(0o777)
    with pytest.raises(ValueError, match="inseguro"):
        write_private(tmp_path / "key", b"never-written", "root")
    assert not (tmp_path / "key").exists()


def test_installed_gunicorn_preserves_inherited_socket_across_restarts(tmp_path):
    import json
    import os
    import socket
    import subprocess
    import sys
    import time
    from pathlib import Path

    if os.geteuid() == 0:
        pytest.skip("Installed control services deliberately refuse root.")
    (tmp_path / "keys").mkdir()
    key = tmp_path / "keys/django.key"
    key.write_text("a" * 64)
    key.chmod(0o600)
    path = tmp_path / "panel.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(path))
    listener.listen(16)
    inode = path.stat().st_ino
    root = Path(__file__).resolve().parents[1]
    code = f"""
import os, sys
sys.path.insert(0, {str(root / "backend")!r})
from deployment import serve
from deployment.config import environment
os.dup2({listener.fileno()}, 3)
os.environ.update(LISTEN_PID=str(os.getpid()), LISTEN_FDS='1')
config={json.dumps(CONFIG)!r}
import json
config=json.loads(config)
serve.load=lambda:config
serve.environment=lambda c:dict(environment(c), SHINYD_STATE_DIR={str(tmp_path)!r})
sys.argv=['service','panel']
serve.main()
"""
    try:
        for _ in range(2):
            process = subprocess.Popen(
                [sys.executable, "-c", code],
                pass_fds=(listener.fileno(),),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    assert process.poll() is None, "Gunicorn exited during startup"
                    try:
                        with socket.socket(socket.AF_UNIX) as client:
                            client.settimeout(0.5)
                            client.connect(str(path))
                            client.sendall(
                                b"GET /healthz HTTP/1.0\r\nHost: panel.example.org\r\n"
                                b"X-Forwarded-Proto: https\r\n\r\n"
                            )
                            if b"200 OK" in client.recv(1024):
                                break
                    except (OSError, TimeoutError):
                        pass
                else:
                    pytest.fail("Inherited listener did not serve healthz")
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            assert process.returncode == 0
            assert path.exists() and path.stat().st_ino == inode
    finally:
        listener.close()


@pytest.mark.parametrize("existing", ["certificate", "configuration", "data", "dangling_link"])
def test_installed_probe_refuses_existing_or_partial_installation(tmp_path, existing):
    from installed_browser_probe import require_empty_control

    state = tmp_path / "state"
    state.mkdir()
    config, certificate = tmp_path / "config", tmp_path / "cert"
    require_empty_control(state, [config, certificate])
    if existing == "dangling_link":
        certificate.symlink_to(tmp_path / "absent")
    else:
        target = {
            "certificate": certificate,
            "configuration": config,
            "data": state / "metadata.sqlite3",
        }[existing]
        target.write_bytes(b"operator-data")
    with pytest.raises(RuntimeError, match="installed_control_not_empty"):
        require_empty_control(state, [config, certificate])
    if existing == "dangling_link":
        assert certificate.is_symlink()
    else:
        assert target.read_bytes() == b"operator-data"

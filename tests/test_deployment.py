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

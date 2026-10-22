"""First-install assistant: validate inputs, retain keys and prepare metadata."""

import argparse
import grp
import json
import os
import stat
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from deployment.config import CONFIG, STATE, caddyfile, load, validate


def verify_certificate(certificate, key, config, *, require_current=True):
    cert = x509.load_pem_x509_certificate(certificate)
    private = serialization.load_pem_private_key(key, password=None)
    encoding, form = serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    if cert.public_key().public_bytes(encoding, form) != private.public_key().public_bytes(
        encoding, form
    ):
        raise ValueError("Certificado e chave não correspondem.")
    now = datetime.now(UTC)
    if require_current and not cert.not_valid_before_utc <= now < cert.not_valid_after_utc:
        raise ValueError("Certificado fora do período de validade.")
    public = cert.public_key()
    if not (
        (isinstance(public, rsa.RSAPublicKey) and public.key_size >= 2048)
        or (isinstance(public, ec.EllipticCurvePublicKey) and public.key_size >= 256)
    ):
        raise ValueError("Use certificado RSA de ao menos 2048 bits ou EC de ao menos 256 bits.")
    names = cert.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value.get_values_for_type(x509.DNSName)
    panel = config["panel_host"]
    covers_panel = panel in names or "*." + panel.split(".", 1)[1] in names
    if not covers_panel or "*." + config["app_domain"] not in names:
        raise ValueError("O certificado deve cobrir o painel e *.domínio-dos-apps.")


def write_private(path, data, group, *, mode=0o640):
    # Parents come from package tmpfiles, owned by root and never service-writable.
    parent = path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != 0 or parent.st_mode & 0o022:
        raise ValueError("Diretório de configuração inseguro.")
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            os.fchmod(stream.fileno(), mode)
            os.fchown(stream.fileno(), 0, grp.getgrnam(group).gr_gid)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="Configura serviços instalados sem iniciá-los.")
    parser.add_argument("--panel-host", required=True)
    parser.add_argument("--app-domain", required=True)
    parser.add_argument("--certificate", required=True, type=Path)
    parser.add_argument("--private-key", required=True, type=Path)
    parser.add_argument("--https-port", type=int, default=443)
    parser.add_argument("--bind", default="0.0.0.0")
    args = parser.parse_args()
    stage = "pré-requisitos"
    try:
        if os.geteuid() != 0:
            raise ValueError("Execute a configuração com sudo.")
        config = validate(
            dict(
                schema_version=1,
                panel_host=args.panel_host,
                app_domain=args.app_domain,
                https_port=args.https_port,
                bind=args.bind,
            )
        )
        if CONFIG.exists() and load() != config:
            raise ValueError(
                "Configuração existente difere; mudança de domínio exige migração explícita."
            )
        active = subprocess.run(
            [
                "systemctl",
                "is-active",
                "--quiet",
                "shinyd.target",
                *[
                    f"shinyd-{name}.service"
                    for name in ("panel", "auth", "worker", "gateway", "ingress", "maintenance")
                ],
                *[f"shinyd-{name}.socket" for name in ("panel", "auth", "gateway")],
                "shinyd-maintenance.timer",
            ]
        )
        if active.returncode == 0:
            raise ValueError("Pare shinyd.target em janela de manutenção antes de configurar.")
        for policy in ("/etc/shinyd-builder/policy.json", "/etc/shinyd-runtime/policy.json"):
            if not Path(policy).is_file():
                raise ValueError(
                    "Prepare as políticas aprovadas de builder/runtime antes de configurar."
                )
        stage = "certificado"
        cert, key = args.certificate.read_bytes(), args.private_key.read_bytes()
        verify_certificate(cert, key, config)
        write_private(Path("/etc/shinyd-tls/cert.pem"), cert, "shinyd-ingress")
        write_private(Path("/etc/shinyd-tls/key.pem"), key, "shinyd-ingress")
        write_private(
            Path("/etc/shinyd-tls/Caddyfile"), caddyfile(config).encode(), "shinyd-ingress"
        )
        stage = "validação HTTPS"
        result = subprocess.run(
            [
                "runuser",
                "-u",
                "shinyd-ingress",
                "--",
                "/usr/bin/caddy",
                "validate",
                "--config",
                "/etc/shinyd-tls/Caddyfile",
                "--adapter",
                "caddyfile",
            ],
            capture_output=True,
            timeout=30,
        )
        if result.returncode:
            raise ValueError("Caddy recusou a configuração HTTPS.")
        stage = "configuração e migrações"
        write_private(CONFIG, json.dumps(config, indent=2).encode() + b"\n", "shinyd-config")
        subprocess.run(
            [
                "runuser",
                "-u",
                "shinyd-control",
                "--",
                "/usr/bin/python3",
                "-I",
                "/usr/lib/shinyd-control/launch.py",
                "prepare",
            ],
            check=True,
            timeout=120,
        )
        write_private(
            Path("/etc/shinyd-gateway/key"),
            (STATE / "keys/gateway.key").read_bytes(),
            "shinyd-gateway",
        )
        print("Configuração preparada. Chaves preservadas; serviços ainda não iniciados.")
        print(
            "Crie a conta com sudo shinyd-admin createsuperuser e inicie com "
            "sudo systemctl enable --now shinyd.target."
        )
    except (ValueError, OSError, subprocess.SubprocessError, x509.ExtensionNotFound):
        # Do not serialize input values or subprocess output (may contain credentials).
        raise SystemExit(
            f"Configuração não concluída na etapa {stage}. "
            "Confira nomes, certificado/chave, políticas e "
            "serviços parados; consulte o guia de instalação."
        ) from None

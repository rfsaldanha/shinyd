"""Fixed local manual-test profile; public artifacts contain no credentials."""

import base64
import hashlib
import io
import json
import os
import re
import stat
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = Path("/usr/local/share/shinyd-manual-test")
PRIVATE = Path("/var/lib/shinyd-manual-test")
CONFIG = {
    "schema_version": 1,
    "panel_host": "panel.shinyd.test",
    "app_domain": "apps.shinyd.test",
    "https_port": 18443,
    "bind": "127.0.0.1",
}
ORIGIN = "https://panel.shinyd.test:18443"
PACKAGES = {
    "shinyd-agent": "0.1.0~agent1",
    "shinyd-runtime": "0.1.0~runtime5",
    "shinyd-builder": "0.1.0~builder2",
    "shinyd-control": "0.1.0~control10",
}


def read_file(path, *, owners, limit=8 * 1024**2, private=False):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid not in owners
            or info.st_mode & (0o077 if private else 0o022)
            or info.st_size > limit
        ):
            raise ValueError("unsafe_file")
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("file_limit")
        return data


def plan_from_report(raw):
    report = json.loads(raw)
    if not isinstance(report, dict):
        raise ValueError("successful_backup_report_required")
    required = {
        "build_0_prepared",
        "build_1_prepared",
        "different_r_and_packages",
        "backup_empty_installation_restored",
        "backup_restored_shiny_reactive_with_data_and_secrets",
    }
    tests = report.get("tests", [])
    if (
        report.get("passed") is not True
        or report.get("error")
        or report.get("cleanup_error")
        or not isinstance(tests, list)
        or not tests
        or any(not isinstance(t, dict) or t.get("passed") is not True for t in tests)
        or not required <= {t.get("name") for t in tests}
        or not isinstance(report.get("installed"), dict)
        or report["installed"].get("passed") is not True
    ):
        raise ValueError("successful_backup_report_required")
    artifacts = report.get("artifacts", [])
    references = json.loads((ROOT / "experiments/r-images.json").read_text())["images"]
    if not isinstance(artifacts, list) or len(artifacts) != len(references):
        raise ValueError("two_test_environments_required")
    environments = {}
    for index, (artifact, reference) in enumerate(zip(artifacts, references, strict=True)):
        if not isinstance(artifact, dict):
            raise ValueError("invalid_observed_environment")
        image, version, packages = (
            artifact["base_image"],
            artifact["r_version"],
            artifact["packages"],
        )
        if (
            not isinstance(image, str)
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", image)
            or version != reference["r"]
            or not isinstance(packages, dict)
            or not 1 <= len(packages) <= 2000
            or not {"shiny", "renv", "jsonlite"} <= packages.keys()
            or any(
                not re.fullmatch(r"[A-Za-z][A-Za-z0-9.]{0,99}", k)
                or not isinstance(v, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+-]{0,63}", v)
                for k, v in packages.items()
            )
        ):
            raise ValueError("invalid_observed_environment")
        environments["r-" + chr(97 + index)] = {
            "image": image,
            "r_version": version,
            "packages": packages,
        }
    return {
        "source_report_sha256": hashlib.sha256(raw).hexdigest(),
        "config": CONFIG,
        "builder": {"schema_version": 1, "environments": environments},
        "runtime": {
            "schema_version": 1,
            "recipes": {},
            "builder_artifacts": True,
            "max_instances": 5,
            "memory_budget_mib": 4096,
        },
    }


def certificate_pair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "shinyd local manual test")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=5))
        .not_valid_after(datetime.now(UTC) + timedelta(days=90))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName(CONFIG["panel_host"]),
                    x509.DNSName("*." + CONFIG["app_domain"]),
                ]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM), key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


def public_manifest(plan, pem):
    cert = x509.load_pem_x509_certificate(pem)
    spki = cert.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return {
        "schema_version": 1,
        "configured": True,
        "mode": "local-manual-test",
        "origin": ORIGIN,
        "config": CONFIG,
        "source_report_sha256": plan["source_report_sha256"],
        "certificate_sha256": hashlib.sha256(pem).hexdigest(),
        "spki": base64.b64encode(hashlib.sha256(spki).digest()).decode(),
        "certificate_expires_at": cert.not_valid_after_utc.isoformat(),
        "environments": {
            name: {
                "r_version": value["r_version"],
                "shiny_version": value["packages"]["shiny"],
                "packages": value["packages"],
            }
            for name, value in plan["builder"]["environments"].items()
        },
        "sample_zips": [
            f"demo-{env}-{revision}.zip" for env in ("r-a", "r-b") for revision in ("v1", "v2")
        ],
        "accounts_created_by_setup": False,
        "services_started_by_setup": False,
    }


def sample_zip(environment, revision):
    if revision not in {"v1", "v2"}:
        raise ValueError("invalid_sample_revision")
    code = (ROOT / "experiments/manual-fixture/app.R").read_text()
    code = code.replace('REVISAO <- "v1"', 'REVISAO <- "' + revision + '"')
    lock = {
        "R": {
            "Version": environment["r_version"],
            "Repositories": [
                {"Name": "CRAN", "URL": "https://cloud.r-project.org"},
            ],
        },
        "Packages": {
            name: {
                "Package": name,
                "Version": version,
                "Source": "Repository",
                "Repository": "CRAN",
            }
            for name, version in environment["packages"].items()
        },
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in (("app.R", code), ("renv.lock", json.dumps(lock, indent=2))):
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)
    return output.getvalue()

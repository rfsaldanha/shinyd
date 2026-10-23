#!/usr/bin/env python3
"""Build Ubuntu 24.04 amd64 package, including pinned Python binary dependencies."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PACKAGES = {
    "argon2-cffi",
    "argon2-cffi-bindings",
    "asgiref",
    "cffi",
    "cryptography",
    "Django",
    "gunicorn",
    "packaging",
    "pycparser",
    "sqlparse",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--go", default="go")
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 12):
        parser.error("Use Python 3.12 para gerar o pacote Ubuntu 24.04.")
    output = ROOT / "dist/shinyd-control_0.1.0~control10_amd64.deb"
    output.parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="shinyd-control-package-") as temporary:
        work = Path(temporary)
        target = work / "package"
        lib = target / "usr/lib/shinyd-control"
        lib.mkdir(parents=True)
        requirements = work / "requirements.txt"
        requirements.write_text(
            "\n".join(
                line
                for line in (ROOT / "requirements.lock").read_text().splitlines()
                if line.split("==")[0] in RUNTIME_PACKAGES
            )
            + "\n"
        )
        wheels = work / "wheels"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "--isolated",
                "download",
                "--only-binary=:all:",
                "--no-deps",
                "--dest",
                str(wheels),
                "-r",
                str(requirements),
            ],
            check=True,
            timeout=180,
        )
        manifest = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(wheels.iterdir())
        }
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "--isolated",
                "install",
                "--no-compile",
                "--no-index",
                "--find-links",
                str(wheels),
                "--target",
                str(lib / "python"),
                "-r",
                str(requirements),
            ],
            check=True,
            timeout=120,
        )
        shutil.rmtree(lib / "python/bin", ignore_errors=True)
        shutil.copytree(
            ROOT / "backend", lib / "backend", ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
        )
        shutil.copytree(ROOT / "backend/control/static", lib / "static")
        for name in ("launch.py", "configure.py", "backup.py"):
            shutil.copyfile(ROOT / "packaging/control" / name, lib / name)
        (lib / "dependency-hashes.json").write_text(json.dumps(manifest, indent=2) + "\n")
        (lib / "requirements.lock").write_bytes(requirements.read_bytes())
        for source, dest in [
            ("debian", "DEBIAN"),
            ("systemd", "usr/lib/systemd/system"),
            ("sysusers", "usr/lib/sysusers.d"),
            ("tmpfiles", "usr/lib/tmpfiles.d"),
        ]:
            shutil.copytree(ROOT / "packaging/control" / source, target / dest)
        # A durable guard prevents socket/timer/reboot activation during a cold restore.
        for unit in (
            "shinyd.target",
            "shinyd-maintenance.timer",
            "shinyd-build@.service",
            *[
                f"shinyd-{n}.service"
                for n in (
                    "panel",
                    "auth",
                    "worker",
                    "gateway",
                    "ingress",
                    "maintenance",
                    "runtime",
                    "builder",
                )
            ],
            *[f"shinyd-{n}.socket" for n in ("panel", "auth", "gateway", "runtime", "builder")],
        ):
            directory = target / "usr/lib/systemd/system" / (unit + ".d")
            directory.mkdir()
            (directory / "backup.conf").write_text(
                "[Unit]\nConditionPathExists=!/var/lib/shinyd-backup/maintenance\n"
                "ConditionPathExists=!/var/lib/shinyd-backup/restore-incomplete\n"
            )
        for name in ("shinyd-admin", "shinyd-configure", "shinyd-backup"):
            path = target / "usr/bin" / name
            path.parent.mkdir(exist_ok=True, parents=True)
            shutil.copyfile(ROOT / "packaging/control" / name, path)
        binary = target / "usr/libexec/shinyd/shinyd-gateway"
        binary.parent.mkdir(parents=True)
        subprocess.run(
            [args.go, "build", "-trimpath", "-o", str(binary), "./cmd/shinyd-gateway"],
            cwd=ROOT,
            env={**os.environ, "CGO_ENABLED": "0", "GOOS": "linux", "GOARCH": "amd64"},
            check=True,
            timeout=120,
        )
        docs = target / "usr/share/doc/shinyd-control"
        docs.mkdir(parents=True)
        shutil.copyfile(ROOT / "LICENSE", docs / "copyright")
        shutil.copyfile(ROOT / "docs/INSTALACAO_INTEGRADA.md", docs / "README.md")
        shutil.copyfile(ROOT / "docs/BACKUP_RESTAURACAO.md", docs / "BACKUP_RESTAURACAO.md")
        target.chmod(0o755)
        for path in target.rglob("*"):
            path.chmod(
                0o755
                if path.is_dir()
                or path.name
                in {
                    "postinst",
                    "prerm",
                    "postrm",
                    "shinyd-admin",
                    "shinyd-configure",
                    "shinyd-backup",
                    "shinyd-gateway",
                }
                else 0o644
            )
        subprocess.run(
            ["dpkg-deb", "--root-owner-group", "--build", str(target), str(output)],
            env={**os.environ, "SOURCE_DATE_EPOCH": "1792454400"},
            check=True,
            timeout=120,
        )
    sha = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(".deb.sha256").write_text(f"{sha}  {output.name}\n")
    print(output)


if __name__ == "__main__":
    main()

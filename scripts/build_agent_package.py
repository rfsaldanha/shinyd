#!/usr/bin/env python3
"""Build the diagnostic agent .deb without installing or touching host services."""

import argparse
import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def verify_units(staging, component="agent"):
    """Check the exact packaged units/binary against the installed systemd parser."""
    with tempfile.TemporaryDirectory(prefix="shinyd-unit-verification-") as temporary:
        root = Path(temporary)
        shutil.copytree(staging / "usr", root / "usr")
        units = root / "usr/lib/systemd/system"
        if component == "runtime":
            shutil.copyfile(
                ROOT / "packaging/systemd/shinyd-control.slice", units / "shinyd-control.slice"
            )
        # Dependency targets only in this temporary verification root, never in
        # the package. --root avoids reading this host's custom service units.
        for source in Path("/usr/lib/systemd/system").glob("*.target"):
            if source.is_file():
                shutil.copyfile(source, units / source.name)
        subprocess.run(
            [
                "systemd-analyze",
                "verify",
                "--man=no",
                "--generators=no",
                "--root",
                str(root),
                f"shinyd-{component}.socket",
                f"shinyd-{component}.service",
                "shinyd-control.slice",
            ],
            check=True,
            timeout=30,
        )


def build(go, output, component="agent"):
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "CGO_ENABLED": "0", "GOOS": "linux", "GOARCH": "amd64"}
    # Stable archive timestamps; the Go build ID still identifies actual source.
    env.setdefault("SOURCE_DATE_EPOCH", "1792454400")
    with tempfile.TemporaryDirectory(prefix="shinyd-agent-package-") as temporary:
        staging = Path(temporary)
        for binary, directory in (
            (f"shinyd-{component}", "usr/libexec/shinyd"),
            (f"shinyd-{component}ctl", "usr/bin"),
        ):
            target = staging / directory / binary
            target.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [go, "build", "-trimpath", "-o", str(target), f"./cmd/{binary}"],
                cwd=ROOT,
                env=env,
                check=True,
                timeout=120,
            )
        for source_dir, destination in (
            ("systemd", "usr/lib/systemd/system"),
            ("sysusers", "usr/lib/sysusers.d"),
            ("tmpfiles", "usr/lib/tmpfiles.d"),
            ("debian", "DEBIAN"),
        ):
            source = ROOT / "packaging"
            if component == "runtime":
                source = source / "runtime"
            shutil.copytree(source / source_dir, staging / destination)
        for name in ("postinst", "prerm", "postrm"):
            (staging / "DEBIAN" / name).chmod(0o755)
        doc = staging / f"usr/share/doc/shinyd-{component}"
        doc.mkdir(parents=True)
        shutil.copyfile(ROOT / "LICENSE", doc / "copyright")
        document = "AGENTE_EXECUCAO.md" if component == "agent" else "EXECUTOR_CONTAINERS.md"
        shutil.copyfile(ROOT / "docs" / document, doc / "README.md")
        staging.chmod(0o755)
        executables = {
            "postinst",
            "prerm",
            "postrm",
            f"shinyd-{component}",
            f"shinyd-{component}ctl",
        }
        for path in staging.rglob("*"):
            path.chmod(0o755 if path.is_dir() or path.name in executables else 0o644)
        verify_units(staging, component)
        package = output / f"shinyd-{component}_0.1.0~{component}1_amd64.deb"
        subprocess.run(
            ["dpkg-deb", "--root-owner-group", "--build", str(staging), str(package)],
            env=env,
            check=True,
            timeout=60,
        )
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    package.with_suffix(".deb.sha256").write_text(f"{digest}  {package.name}\n")
    return package


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--go", default="go", help="Executável Go")
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument("--component", choices=("agent", "runtime"), default="agent")
    args = parser.parse_args()
    print(build(args.go, args.output, args.component))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Package the offline builder without installing it or changing host services."""

import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    output = ROOT / "dist/shinyd-builder_0.1.0~builder1_amd64.deb"
    output.parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="shinyd-builder-package-") as directory:
        target = Path(directory)
        lib = target / "usr/lib/shinyd-builder"
        lib.mkdir(parents=True)
        shutil.copytree(
            ROOT / "backend/builder", lib / "builder", ignore=shutil.ignore_patterns("__pycache__")
        )
        (lib / "control").mkdir()
        (lib / "control/__init__.py").touch()
        shutil.copyfile(ROOT / "backend/control/archives.py", lib / "control/archives.py")
        shutil.copyfile(ROOT / "packaging/builder/main.py", lib / "main.py")
        binary = target / "usr/bin/shinyd-build"
        binary.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / "packaging/builder/shinyd-build", binary)
        shutil.copytree(ROOT / "packaging/builder/debian", target / "DEBIAN")
        shutil.copytree(ROOT / "packaging/builder/systemd", target / "usr/lib/systemd/system")
        docs = target / "usr/share/doc/shinyd-builder"
        docs.mkdir(parents=True)
        shutil.copyfile(ROOT / "LICENSE", docs / "copyright")
        shutil.copyfile(ROOT / "docs/BUILDER_OFFLINE.md", docs / "README.md")
        target.chmod(0o755)
        for path in target.rglob("*"):
            path.chmod(
                0o755
                if path.is_dir() or path.name in {"shinyd-build", "postinst", "postrm"}
                else 0o644
            )
        subprocess.run(
            ["dpkg-deb", "--root-owner-group", "--build", str(target), str(output)],
            env={**os.environ, "SOURCE_DATE_EPOCH": "1792454400"},
            check=True,
            timeout=60,
        )
    sha = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(".deb.sha256").write_text(f"{sha}  {output.name}\n")
    print(output)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Create local key material without overwriting an existing installation."""

import argparse
import os
import secrets
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--state-dir", type=Path, default=Path(__file__).resolve().parents[1] / "var")
args = parser.parse_args()
os.umask(0o077)
root = args.state_dir.resolve()
root.mkdir(mode=0o700, parents=True, exist_ok=True)
for directory in ("keys", "sources", "staging"):
    (root / directory).mkdir(mode=0o700, exist_ok=True)
for name, value in (
    ("django.key", secrets.token_urlsafe(64).encode()),
    ("secrets.key", secrets.token_bytes(32)),
    ("gateway.key", secrets.token_hex(32).encode()),
):
    path = root / "keys" / name
    try:
        with path.open("xb") as file:
            file.write(value)
    except FileExistsError:
        pass
print(f"Estado preparado em {root}. Chaves existentes preservadas; valores não exibidos.")

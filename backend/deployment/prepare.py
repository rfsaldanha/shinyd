"""Run only as the control identity, before starting the installed services."""

import os
import secrets
import sqlite3
from datetime import UTC, datetime

from deployment.config import STATE


def prepare():
    os.umask(0o077)
    for name in ("keys", "sources", "staging", "backups"):
        (STATE / name).mkdir(mode=0o700, exist_ok=True)
    for name, value in (
        ("django.key", secrets.token_urlsafe(64).encode()),
        ("gateway.key", secrets.token_hex(32).encode()),
        ("secrets.key", secrets.token_bytes(32)),
    ):
        try:
            with (STATE / "keys" / name).open("xb") as output:
                output.write(value)
        except FileExistsError:
            pass
    database = STATE / "metadata.sqlite3"
    if database.exists():
        backup = (
            STATE
            / "backups"
            / ("metadata-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + ".sqlite3")
        )
        with sqlite3.connect(database) as source, sqlite3.connect(backup) as target:
            source.backup(target)
        print("Backup privado de metadados criado antes das migrações.")
    from django.core.management import execute_from_command_line

    execute_from_command_line(["shinyd-admin", "migrate", "--noinput"])
    execute_from_command_line(["shinyd-admin", "check", "--deploy", "--fail-level", "WARNING"])

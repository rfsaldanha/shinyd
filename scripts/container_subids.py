#!/usr/bin/env python3
"""Inspect or explicitly reserve subordinate IDs for rootful Podman userns=auto."""

import argparse
import grp
import json
import os
import pwd
import shutil
import stat
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

OWNER = "containers"
BLOCK = 65536
POOL_SIZE = 16 * BLOCK
ID_END = 2**32 - 1  # Exclusive; (uid_t)-1 is not a usable identity.


def read_regular(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o022:
            raise ValueError(
                f"Arquivo deve ser regular, único e sem escrita de grupo/outros: {path}"
            )
        return stream.read()


def parse_ranges(data, name):
    entries = []
    for number, line in enumerate(data.decode("utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split(":")
        if (
            len(fields) != 3
            or not fields[0]
            or any(char.isspace() for char in fields[0])
            or not fields[1].isascii()
            or not fields[1].isdigit()
            or not fields[2].isascii()
            or not fields[2].isdigit()
        ):
            raise ValueError(f"Entrada inválida em {name}:{number}; nenhuma correção automática.")
        start, size = map(int, fields[1:])
        if start < 1 or size < 1 or start + size > ID_END:
            raise ValueError(f"Faixa inválida em {name}:{number}.")
        entries.append((fields[0], start, start + size))
    return entries


def check_provider(etc):
    text = read_regular(etc / "nsswitch.conf").decode("utf-8")
    for line in text.splitlines():
        key, separator, value = line.partition("#")[0].partition(":")
        if separator and key.strip() == "subid" and value.split() != ["files"]:
            raise ValueError("Provedor subid externo: configuração automática somente para files.")


def overlap(a, b):
    return a[0] < b[1] and b[0] < a[1]


def plan_reservation(contents, user_ids, group_ids):
    parsed = {name: parse_ranges(data, name) for name, data in contents.items()}
    existing = {
        name: sorted((start, end) for owner, start, end in entries if owner == OWNER)
        for name, entries in parsed.items()
    }
    identity_ranges = {
        "subuid": [(value, value + 1) for value in user_ids],
        "subgid": [(value, value + 1) for value in group_ids],
    }
    if any(existing.values()):
        # Never move/resize an existing mapping: files may already belong to those IDs.
        if existing["subuid"] != existing["subgid"]:
            raise ValueError("Faixas containers incompletas ou diferentes; exige revisão manual.")
        for name, ranges in existing.items():
            others = [
                (start, end) for owner, start, end in parsed[name] if owner != OWNER
            ] + identity_ranges[name]
            for index, candidate in enumerate(ranges):
                if any(overlap(candidate, other) for other in others + ranges[:index]):
                    raise ValueError(
                        f"Faixa containers sobreposta em {name}; exige revisão manual."
                    )
        if not any(end - start >= BLOCK for start, end in existing["subuid"]):
            raise ValueError("Reserva containers existente menor que 65536; não será ampliada.")
        return {
            "action": "unchanged",
            "ranges": [{"start": start, "count": end - start} for start, end in existing["subuid"]],
            "additions": {},
        }

    occupied = sorted(
        [(start, end) for entries in parsed.values() for _, start, end in entries]
        + identity_ranges["subuid"]
        + identity_ranges["subgid"]
    )
    start = BLOCK
    for low, high in occupied:
        if high <= start:
            continue
        if start + POOL_SIZE <= low:
            break
        start = ((high + BLOCK - 1) // BLOCK) * BLOCK
    if start + POOL_SIZE > ID_END:
        raise ValueError("Não há faixa livre suficiente; nenhuma alteração realizada.")
    line = f"{OWNER}:{start}:{POOL_SIZE}\n"
    return {
        "action": "reserve",
        "ranges": [{"start": start, "count": POOL_SIZE}],
        "additions": {"subuid": line, "subgid": line},
    }


def inspect_reservation(etc=Path("/etc")):
    check_provider(etc)
    contents = {name: read_regular(etc / name) for name in ("subuid", "subgid")}
    # NSS enumeration includes identities visible through the configured providers.
    plan = plan_reservation(
        contents,
        [user.pw_uid for user in pwd.getpwall()],
        [group.gr_gid for group in grp.getgrall()],
    )
    return plan, contents


@contextmanager
def account_locks(etc):
    """Use shadow's exclusive .lock convention; never remove another owner's lock."""
    owned = []
    try:
        for name in ("passwd", "group", "subuid", "subgid"):
            path = etc / (name + ".lock")
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError as exc:
                raise RuntimeError(
                    f"Cadastro ocupado: {path}. Repita após a operação terminar."
                ) from exc
            owned.append(path)
            with os.fdopen(fd, "wb") as stream:
                stream.write(f"{os.getpid()}\0".encode())
                stream.flush()
                os.fsync(stream.fileno())
        yield
    finally:
        for path in reversed(owned):
            path.unlink()


def atomic_replace(path, data):
    info = path.stat(follow_symlinks=False)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".shinyd-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            if os.geteuid() == 0:
                os.fchown(stream.fileno(), info.st_uid, info.st_gid)
            os.fchmod(stream.fileno(), stat.S_IMODE(info.st_mode))
            shutil.copystat(path, temporary, follow_symlinks=False)
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def reserve(etc=Path("/etc")):
    with account_locks(etc):
        plan, contents = inspect_reservation(etc)
        if plan["action"] == "unchanged":
            return {**plan, "applied": False}
        backup = Path(tempfile.mkdtemp(prefix="shinyd-subids-backup-", dir=etc))
        # Make both originals durable before replacing either file.
        for name, data in contents.items():
            with (backup / name).open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        for directory in (backup, etc):
            fd = os.open(directory, os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        written = []
        try:
            for name, line in plan["additions"].items():
                path = etc / name
                if read_regular(path) != contents[name]:
                    raise RuntimeError(f"{path} mudou durante a preparação.")
                original = contents[name]
                separator = b"" if not original or original.endswith(b"\n") else b"\n"
                atomic_replace(path, original + separator + line.encode())
                written.append(name)
            fd = os.open(etc, os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except BaseException as exc:
            try:
                for name in reversed(written):
                    atomic_replace(etc / name, contents[name])
            except OSError as rollback_error:
                raise RuntimeError(
                    f"Falha e restauração incompleta; originais em {backup}: {rollback_error}"
                ) from exc
            raise RuntimeError(f"Preparação interrompida; originais em {backup}: {exc}") from exc
        return {**plan, "applied": True, "backup_directory": str(backup)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Grava a reserva; exige sudo.")
    args = parser.parse_args()
    if args.apply and os.geteuid() != 0:
        parser.error("--apply exige root.")
    try:
        result = reserve() if args.apply else {**inspect_reservation()[0], "applied": False}
    except (ValueError, RuntimeError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

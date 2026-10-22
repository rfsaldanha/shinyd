"""Private, bounded backup staging. Never extract tar paths with tarfile.extract()."""

import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
from contextlib import ExitStack
from pathlib import Path, PurePosixPath

from builder.policy import strict_json

AGE = "/usr/bin/age"
LIMIT = 64 * 1024**3
ENTRIES = 100_000
MANIFEST_LIMIT = 16 * 1024**2
RESERVE = 1024**3
UUID = r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}"
HASH = r"[a-f0-9]{64}"
FIXED = {
    "control/metadata.sqlite3",
    *["control/keys/" + n + ".key" for n in ("django", "gateway", "secrets")],
    *[
        "config/" + n
        for n in ("control.json", "runtime.json", "builder.json", "cert.pem", "tls.key")
    ],
}
DIRECTORIES = {
    "control",
    "control/keys",
    "control/sources",
    "config",
    "data",
    "builder",
    "builder/artifacts",
    "runtime",
    "images",
}


class BackupError(Exception):
    """Messages are fixed and may be displayed; never include paths or file contents."""


def allowed(name, directory=False):
    if not isinstance(name, str) or len(name.encode()) > 1024 or "\\" in name:
        return False
    parts = name.split("/")
    if any(p in {"", ".", ".."} or any(ord(c) < 32 for c in p) for p in parts):
        return False
    if directory and name in DIRECTORIES:
        return True
    if name.startswith("data/"):
        return bool(re.fullmatch(UUID, parts[1])) and (
            (len(parts) == 2 and directory)
            or (len(parts) >= 3 and parts[2] == "files" and (len(parts) > 3 or directory))
        )
    return not directory and (
        name in FIXED
        or re.fullmatch(rf"control/sources/{UUID}\.zip", name) is not None
        or re.fullmatch(rf"builder/artifacts/{HASH}\.json", name) is not None
        or re.fullmatch(rf"runtime/{UUID}\.json", name) is not None
        or re.fullmatch(rf"images/{HASH}\.tar", name) is not None
    )


def space(path, amount):
    if shutil.disk_usage(path).free < amount + RESERVE:
        raise BackupError("Espaço insuficiente; mantenha ao menos 1 GiB livre além da operação.")


def regular(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        raise BackupError("Arquivo ausente, inacessível ou link inseguro.") from None
    stream = os.fdopen(fd, "rb")
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        stream.close()
        raise BackupError("Links e arquivos especiais não são aceitos no backup.")
    return stream


def copy_file(source, target, limit=LIMIT, created=None):
    with regular(source) as src:
        before = os.fstat(src.fileno())
        if before.st_size > limit:
            raise BackupError("Arquivo excede o limite do backup.")
        space(target.parent, before.st_size)
        with target.open("xb") as dst:
            if created is not None:
                created.append(target)
            total = 0
            while block := src.read(1024**2):
                total += len(block)
                if total > limit:
                    raise BackupError("Arquivo excede o limite do backup.")
                dst.write(block)
            dst.flush()
            os.fsync(dst.fileno())
        after = os.fstat(src.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise BackupError("Arquivo mudou durante a cópia; mantenha os serviços parados.")
        target.chmod(stat.S_IMODE(before.st_mode) & 0o700 | 0o600)


def inventory(root, limit=LIMIT):
    result, total = {}, 0
    for base, dirs, files in os.walk(root, followlinks=False):
        for name in sorted(dirs + files):
            path = Path(base) / name
            relative = path.relative_to(root).as_posix()
            info = path.lstat()
            directory = stat.S_ISDIR(info.st_mode)
            if not allowed(relative, directory):
                raise BackupError("Conteúdo fora da estrutura permitida para backup.")
            if directory:
                record = {"kind": "directory", "mode": info.st_mode & 0o777, "size": 0}
            else:
                with regular(path) as stream:
                    record = {
                        "kind": "file",
                        "mode": info.st_mode & 0o777,
                        "size": info.st_size,
                        "sha256": hashlib.file_digest(stream, "sha256").hexdigest(),
                    }
            result[relative] = record
            total += record["size"]
            if len(result) > ENTRIES or total > limit:
                raise BackupError("Backup excede o limite de tamanho ou quantidade de arquivos.")
    return result


def pack(root, output, metadata, limit=LIMIT):
    files = inventory(root, limit)
    manifest = {**metadata, "format": "shinyd-cold-backup-v1", "files": files}
    raw = json.dumps(manifest, sort_keys=True).encode()
    if len(raw) > MANIFEST_LIMIT:
        raise BackupError("Inventário do backup excede o limite.")
    space(output.parent, sum(v["size"] for v in files.values()) + len(files) * 4096)
    with tarfile.open(output, "x", format=tarfile.PAX_FORMAT) as archive:
        item = tarfile.TarInfo("manifest.json")
        item.size, item.mode = len(raw), 0o600
        archive.addfile(item, io.BytesIO(raw))
        for name, record in sorted(files.items()):
            item = tarfile.TarInfo(name)
            item.mode, item.size = record["mode"], record["size"]
            if record["kind"] == "directory":
                item.type = tarfile.DIRTYPE
                archive.addfile(item)
            else:
                with regular(root / name) as source:
                    archive.addfile(item, source)
    if output.stat().st_size > limit:
        raise BackupError("Arquivo final excede o limite do backup.")
    return manifest


def unpack(source, root, limit=LIMIT):
    if source.stat().st_size > limit:
        raise BackupError("Backup excede o limite de tamanho.")
    root.mkdir(mode=0o700)
    with tarfile.open(source, "r|") as archive:
        first = archive.next()
        if (
            first is None
            or first.name != "manifest.json"
            or not first.isfile()
            or not 0 < first.size <= MANIFEST_LIMIT
        ):
            raise BackupError("Inventário ausente ou inválido.")
        manifest = strict_json(archive.extractfile(first).read(MANIFEST_LIMIT + 1))
        if manifest.get("format") != "shinyd-cold-backup-v1":
            raise BackupError("Formato de backup não suportado.")
        files = manifest.get("files")
        if not isinstance(files, dict) or not 1 <= len(files) <= ENTRIES:
            raise BackupError("Inventário inválido.")
        total = 0
        for name, record in files.items():
            if (
                not isinstance(record, dict)
                or record.get("kind") not in {"file", "directory"}
                or not allowed(name, record["kind"] == "directory")
                or type(record.get("size")) is not int
                or record["size"] < 0
                or type(record.get("mode")) is not int
                or not 0 <= record["mode"] <= 0o777
            ):
                raise BackupError("Inventário contém uma entrada inválida.")
            if record["kind"] == "file" and not re.fullmatch(HASH, record.get("sha256", "")):
                raise BackupError("Checksum inválido.")
            if record["kind"] == "directory" and record["size"]:
                raise BackupError("Diretório inválido.")
            total += record["size"]
            if total > limit:
                raise BackupError("Conteúdo excede o limite do backup.")
        if not FIXED <= files.keys():
            raise BackupError("Backup incompleto: faltam arquivos obrigatórios.")
        space(root, total)
        seen = set()
        # archive.next() has already consumed the manifest; iteration includes it once.
        for item in archive:
            if item is first:
                continue
            name = item.name.rstrip("/") if item.isdir() else item.name
            record = files.get(name)
            if (
                name in seen
                or record is None
                or item.issparse()
                or (record["kind"] == "directory" and not item.isdir())
                or (record["kind"] == "file" and not item.isfile())
                or item.size != record["size"]
            ):
                raise BackupError("Arquivo alterado, duplicado ou especial no backup.")
            path = root.joinpath(*PurePosixPath(name).parts)
            # Parents must have appeared as ordinary directories; no implicit traversal.
            if not path.parent.is_dir():
                raise BackupError("Ordem de diretórios inválida.")
            if item.isdir():
                path.mkdir(mode=0o700)
            else:
                with path.open("xb") as output:
                    os.fchmod(output.fileno(), 0o600)
                    shutil.copyfileobj(archive.extractfile(item), output, 1024**2)
                with regular(path) as stream:
                    checksum = hashlib.file_digest(stream, "sha256").hexdigest()
                if checksum != record["sha256"]:
                    raise BackupError("Checksum não corresponde ao inventário.")
            seen.add(name)
        if seen != files.keys():
            raise BackupError("Backup truncado: faltam arquivos do inventário.")
    return manifest


def age(source, output, *, recipient=None, identity=None, limit=LIMIT):
    """Authenticate the whole age stream before parsing or installing any plaintext."""
    import resource

    with ExitStack() as stack:
        descriptors = ()
        if recipient is not None:
            if not re.fullmatch(r"age1[0-9a-z]{58}", recipient):
                raise BackupError("Use uma chave pública age X25519 (age1…).")
            args = [AGE, "--encrypt", "--recipient", recipient]
        else:
            key = stack.enter_context(regular(identity))
            info = os.fstat(key.fileno())
            if info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_size > 4096:
                raise BackupError("A identidade age deve ser privada (0600) e do operador.")
            raw = key.read(4097).decode("ascii")
            lines = [line for line in raw.splitlines() if line and not line.startswith("#")]
            if len(lines) != 1 or not re.fullmatch(r"AGE-SECRET-KEY-1[0-9A-Z]{58}", lines[0]):
                raise BackupError("Use uma única identidade age X25519, sem plugins.")
            key.seek(0)
            # Pass the validated inode, not an attacker-replaceable pathname.
            descriptors = (key.fileno(),)
            args = [AGE, "--decrypt", "--identity", f"/proc/self/fd/{key.fileno()}"]

        def bound():
            resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))

        source_file = stack.enter_context(regular(source))
        destination = stack.enter_context(output.open("xb"))
        os.fchmod(destination.fileno(), 0o600)
        result = subprocess.run(
            args,
            stdin=source_file,
            stdout=destination,
            stderr=subprocess.DEVNULL,
            timeout=1800,
            preexec_fn=bound,
            pass_fds=descriptors,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        )
        if result.returncode:
            raise BackupError("Criptografia falhou: confira a chave, integridade e espaço livre.")
        destination.flush()
        os.fsync(destination.fileno())

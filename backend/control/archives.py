"""Bounded, streaming extraction. No uploaded code is executed here."""

import hashlib
import json
import re
import stat
import struct
import time
import unicodedata
import zipfile
from pathlib import PurePosixPath


class ArchiveError(Exception):
    pass


def preflight_central_directory(source, max_entries):
    """Bound metadata allocation before ZipFile constructs ZipInfo objects."""
    with open(source, "rb") as stream:
        stream.seek(0, 2)
        size = stream.tell()
        tail_size = min(size, 65557)
        stream.seek(size - tail_size)
        tail = stream.read(tail_size)
        position = tail.rfind(b"PK\x05\x06")
        if position < 0 or len(tail) - position < 22:
            raise ArchiveError("ZIP sem diretório central válido.")
        _, disk, central_disk, disk_entries, count, length, offset, comment = struct.unpack(
            "<4s4H2LH", tail[position : position + 22]
        )
        if position + 22 + comment != len(tail) or disk or central_disk or disk_entries != count:
            raise ArchiveError("ZIP dividido ou diretório central inválido.")
        if count == 65535 or length == 0xFFFFFFFF or offset == 0xFFFFFFFF:
            raise ArchiveError("ZIP64 não é necessário para os limites atuais e não é aceito.")
        if count > max_entries or length > 32 * 1024 * 1024:
            raise ArchiveError("ZIP contém metadados ou arquivos demais.")
        if offset + length != size - tail_size + position:
            raise ArchiveError("Limites do diretório central inválidos.")
        stream.seek(offset)
        remaining, actual = length, 0
        while remaining:
            header = stream.read(46)
            if len(header) != 46 or header[:4] != b"PK\x01\x02":
                raise ArchiveError("Diretório central corrompido.")
            name_len, extra_len, comment_len = struct.unpack("<3H", header[28:34])
            record_size = 46 + name_len + extra_len + comment_len
            if record_size > remaining:
                raise ArchiveError("Registro ZIP fora dos limites.")
            remaining -= record_size
            actual += 1
            if actual > max_entries:
                raise ArchiveError("ZIP contém arquivos demais.")
            stream.seek(record_size - 46, 1)
        if actual != count:
            raise ArchiveError("Contagem de entradas ZIP inconsistente.")


def validate_name(info):
    name = info.orig_filename
    if (
        "\x00" in name
        or "\\" in name
        or name.startswith("/")
        or re.match(r"^[A-Za-z]:", name)
        or any(ord(char) < 32 for char in name)
    ):
        raise ArchiveError("ZIP contém um caminho inseguro.")
    parts = name.rstrip("/").split("/")
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ArchiveError("ZIP contém caminho vazio ou tentativa de sair da pasta.")
    if len(name) > 1024 or len(parts) > 32:
        raise ArchiveError("ZIP contém caminho excessivamente longo.")
    mode = info.external_attr >> 16
    kind = stat.S_IFMT(mode)
    if kind not in {0, stat.S_IFREG, stat.S_IFDIR}:
        raise ArchiveError("Links e arquivos especiais não são permitidos no ZIP.")
    if kind == stat.S_IFDIR and not info.is_dir():
        raise ArchiveError("ZIP contém tipo de arquivo inconsistente.")
    if info.flag_bits & 1:
        raise ArchiveError("ZIP protegido por senha não é aceito.")
    return PurePosixPath(*parts)


def inspect_archive(
    source,
    destination,
    *,
    max_bytes,
    max_entries,
    entrypoint="",
    deadline_seconds=60,
    cancelled=lambda: False,
):
    deadline = time.monotonic() + deadline_seconds
    total = 0
    names = set()
    files = set()
    digest = hashlib.sha256()
    try:
        preflight_central_directory(source, max_entries)
        with open(source, "rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        with zipfile.ZipFile(source) as archive:
            entries = archive.infolist()
            if len(entries) > max_entries:
                raise ArchiveError("ZIP contém arquivos demais.")
            if sum(i.file_size for i in entries) > max_bytes:
                raise ArchiveError("O conteúdo descompactado excede o limite.")
            parsed = []
            for info in entries:
                path = validate_name(info)
                normalized = unicodedata.normalize("NFC", str(path)).casefold()
                if normalized in names:
                    raise ArchiveError("ZIP contém nomes duplicados ou ambíguos.")
                names.add(normalized)
                parsed.append((info, path))
                if not info.is_dir():
                    files.add(str(path))
            # Detect file/directory conflicts before writing anything.
            normalized_files = {unicodedata.normalize("NFC", f).casefold() for f in files}
            for _, path in parsed:
                if any(
                    unicodedata.normalize("NFC", str(p)).casefold() in normalized_files
                    for p in path.parents
                    if str(p) != "."
                ):
                    raise ArchiveError("ZIP usa o mesmo caminho como arquivo e pasta.")
            for info, path in parsed:
                if time.monotonic() > deadline or cancelled():
                    raise ArchiveError("Inspeção cancelada ou tempo de extração excedido.")
                target = destination.joinpath(*path.parts)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True, mode=0o700)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                # destination is a new private directory, containing no user-created links.
                with archive.open(info) as src, target.open("xb") as dst:
                    while chunk := src.read(64 * 1024):
                        total += len(chunk)
                        if total > max_bytes:
                            raise ArchiveError("O conteúdo descompactado excede o limite.")
                        if time.monotonic() > deadline or cancelled():
                            raise ArchiveError("Inspeção cancelada ou tempo excedido.")
                        dst.write(chunk)
                target.chmod(0o600)
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError, EOFError) as exc:
        raise ArchiveError("ZIP inválido, corrompido ou com compressão não suportada.") from exc
    candidates = set()
    for name in files:
        path = PurePosixPath(name)
        if path.name == "app.R":
            candidates.add(str(path.parent))
        if path.name == "ui.R" and str(path.with_name("server.R")) in files:
            candidates.add(str(path.parent))
    if not candidates:
        raise ArchiveError("Nenhum app.R ou par ui.R/server.R foi encontrado.")
    if entrypoint:
        if entrypoint not in candidates:
            raise ArchiveError("A pasta selecionada não contém uma entrada Shiny válida.")
        selected = entrypoint
    elif len(candidates) != 1:
        raise ArchiveError("Há mais de um app no ZIP. Informe a pasta desejada ao reenviar.")
    else:
        selected = next(iter(candidates))
    lock_path = destination / selected / "renv.lock"
    r_version = ""
    lock_present = lock_path.is_file()
    if lock_present:
        if lock_path.stat().st_size > 8 * 1024 * 1024:
            raise ArchiveError("renv.lock excede o tamanho permitido.")
        try:
            lock = json.loads(lock_path.read_text())
            r_version = lock["R"]["Version"]
            if not isinstance(r_version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", r_version):
                raise ValueError
            if not isinstance(lock.get("Packages", {}), dict):
                raise ValueError
        except (ValueError, KeyError, TypeError, UnicodeError) as exc:
            raise ArchiveError("renv.lock inválido ou sem versão R exata.") from exc
    return {
        "entrypoint": selected,
        "r_version": r_version,
        "has_lockfile": lock_present,
        "file_count": len(files),
        "uncompressed_bytes": total,
        "source_hash": digest.hexdigest(),
        "warnings": []
        if lock_present
        else ["Sem renv.lock: a restauração reproduzível ainda precisa ser preparada."],
    }

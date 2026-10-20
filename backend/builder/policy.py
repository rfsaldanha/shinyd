"""Local operator policy and strict metadata at the privileged boundary."""

import hashlib
import json
import os
import re
import stat
from pathlib import Path

IMAGE = re.compile(r"sha256:[a-f0-9]{64}\Z")
SLUG = re.compile(r"[a-z][a-z0-9-]{0,39}\Z")
PACKAGE = re.compile(r"[A-Za-z][A-Za-z0-9.]{0,99}\Z")
VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+-]{0,63}\Z")
R_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\Z")
HASH = re.compile(r"[a-f0-9]{32}\Z")


class BuildError(Exception):
    """Only fixed public codes/messages cross the builder boundary."""


MESSAGES = {
    "policy": "Catálogo de ambientes inválido ou com permissões inseguras.",
    "archive": "ZIP inválido, inseguro ou fora dos limites permitidos.",
    "source_changed": "O ZIP não corresponde ao checksum informado.",
    "lock_required": "Este builder offline exige renv.lock na pasta do app.",
    "lock_invalid": "renv.lock inválido ou com fonte de pacotes ainda não suportada.",
    "r_unavailable": "A versão exata do R não corresponde ao ambiente selecionado.",
    "package_unavailable": "Há pacote ou versão ausente no ambiente offline aprovado.",
    "busy": "Outro build está em andamento. Aguarde antes de tentar novamente.",
    "admission": "Build adiado: memória, disco ou controladores indisponíveis.",
    "isolation": "O isolamento do processo não pôde ser comprovado.",
    "assembly": "Falha ao montar a imagem. Verifique armazenamento e runtime.",
    "restore": "Falha na restauração offline. Verifique o lockfile e o ambiente aprovado.",
    "interrupted": "Build interrompido, cancelado ou com prazo excedido.",
    "ownership": "Recurso existente não pertence a este build; operação recusada.",
    "storage": "Falha ao gravar o estado ou o artefato do build.",
    "cleanup": "Limpeza incompleta. O registro foi preservado para recuperação.",
}


def fail(code):
    raise BuildError(code)


def strict_json(raw):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    return json.loads(raw, object_pairs_hook=pairs)


def secure_dir(path, owner=0):
    path = Path(path)
    if not path.is_absolute() or str(path) != os.path.normpath(path):
        fail("policy")
    for ancestor in (path, *path.parents):
        info = ancestor.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, owner) or info.st_mode & 0o022:
            fail("policy")


def private_read(path, *, owner=0, limit=2 * 1024 * 1024):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != owner
            or info.st_nlink != 1
            or info.st_mode & 0o022
            or info.st_size > limit
        ):
            fail("policy")
        raw = stream.read(limit + 1)
        if len(raw) > limit:
            fail("policy")
        return raw


def load_environment(path, name):
    try:
        secure_dir(Path(path).parent)
        policy = strict_json(private_read(path))
        if set(policy) != {"schema_version", "environments"} or policy["schema_version"] != 1:
            fail("policy")
        environments = policy["environments"]
        if not isinstance(environments, dict) or not 1 <= len(environments) <= 32:
            fail("policy")
        for key, value in environments.items():
            if not SLUG.fullmatch(key) or set(value) != {"image", "r_version", "packages"}:
                fail("policy")
            if not IMAGE.fullmatch(value["image"]) or not R_VERSION.fullmatch(value["r_version"]):
                fail("policy")
            packages = value["packages"]
            if not isinstance(packages, dict) or not 1 <= len(packages) <= 2000:
                fail("policy")
            if not {"shiny", "renv", "jsonlite"} <= packages.keys():
                fail("policy")
            for package, version in packages.items():
                if not PACKAGE.fullmatch(package) or not VERSION.fullmatch(version):
                    fail("policy")
        if name not in environments:
            fail("policy")
        return environments[name]
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        fail("policy")


def resolve_lock(path, environment):
    if not path.is_file():
        fail("lock_required")
    try:
        raw = path.read_bytes()
        if len(raw) > 8 * 1024 * 1024:
            fail("lock_invalid")
        lock = strict_json(raw)
        if not R_VERSION.fullmatch(lock["R"]["Version"]):
            fail("lock_invalid")
        if lock["R"]["Version"] != environment["r_version"]:
            fail("r_unavailable")
        packages = lock["Packages"]
        if not isinstance(packages, dict) or not 1 <= len(packages) <= 2000:
            fail("lock_invalid")
        if "shiny" not in packages:
            fail("lock_invalid")
        selected = {}
        for name, record in packages.items():
            if (
                not PACKAGE.fullmatch(name)
                or not isinstance(record, dict)
                or not {"Package", "Version", "Source", "Repository"} <= record.keys()
                or record["Package"] != name
                or not VERSION.fullmatch(record["Version"])
                or record["Source"] != "Repository"
                or record["Repository"] not in {"CRAN", "RSPM"}
                or any(key.startswith(("Remote", "Github", "git")) for key in record)
                or record.get("OS_type", "unix") != "unix"
            ):
                fail("lock_invalid")
            if "Hash" in record and not HASH.fullmatch(record["Hash"]):
                fail("lock_invalid")
            if "Requirements" in record and (
                not isinstance(record["Requirements"], list)
                or len(record["Requirements"]) > 2000
                or any(not PACKAGE.fullmatch(p) for p in record["Requirements"])
            ):
                fail("lock_invalid")
            if environment["packages"].get(name) != record["Version"]:
                fail("package_unavailable")
            # Repository URLs and unrelated project metadata never govern downloads.
            # renv's newer records can contain the full DESCRIPTION. Carry only
            # the restore fields; text such as Authors@R is never evaluated.
            selected[name] = {
                key: record[key]
                for key in ("Package", "Version", "Source", "Repository", "Hash", "Requirements")
                if key in record
            }
        return {
            "R": {
                "Version": environment["r_version"],
                "Repositories": [
                    {"Name": name, "URL": "https://cloud.r-project.org"}
                    for name in sorted({record["Repository"] for record in selected.values()})
                ],
            },
            "Packages": selected,
        }
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        fail("lock_invalid")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()

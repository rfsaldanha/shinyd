"""Small, shared wire contract. No paths, commands, images or secrets from callers."""

import re
import uuid

from .policy import IMAGE, MESSAGES, SLUG, fail

PHASES = {
    "accepted": "ZIP recebido pelo preparador.",
    "recovering": "Limpando uma preparação interrompida.",
    "validating": "Conferindo ZIP, lockfile e ambiente aprovado.",
    "admission": "Verificando recursos disponíveis.",
    "assembling": "Montando a imagem do aplicativo.",
    "restoring": "Preparando a biblioteca privada de pacotes R.",
    "sealing": "Registrando a imagem imutável.",
    "cached": "Artefato encontrado no cache.",
    "prepared": "Imagem preparada. A versão ainda não foi ativada.",
    "failed": "Não foi possível preparar esta versão.",
    "cancelled": "Preparação cancelada.",
}
TERMINAL = {"succeeded", "failed", "cancelled"}
DIGEST = re.compile(r"[a-f0-9]{64}\Z")


def identifier(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def validate_spec(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"app_id", "source_hash", "entrypoint", "environment"}
        or not identifier(value["app_id"])
        or not isinstance(value["source_hash"], str)
        or not DIGEST.fullmatch(value["source_hash"])
        or not isinstance(value["environment"], str)
        or not SLUG.fullmatch(value["environment"])
        or not isinstance(value["entrypoint"], str)
        or len(value["entrypoint"]) > 300
        or (
            value["entrypoint"] not in ("", ".")
            and not re.fullmatch(r"[A-Za-z0-9_. -]+(?:/[A-Za-z0-9_. -]+)*", value["entrypoint"])
        )
        or ".." in value["entrypoint"].split("/")
    ):
        fail("archive")
    return value


def validate_result(value, job_id, spec):
    if (
        not isinstance(value, dict)
        or set(value) != {"id", "spec", "status", "phase", "error", "artifact"}
        or value["id"] != job_id
        or value["spec"] not in (None, spec)
        or (value["spec"] is None and value["status"] != "cancelled")
        or value["status"] not in TERMINAL | {"running"}
        or value["phase"] not in PHASES
        or not isinstance(value["error"], str)
        or value["error"] not in {"", *MESSAGES}
    ):
        raise ValueError("invalid builder response")
    if value["status"] == "succeeded":
        artifact = value["artifact"]
        if (
            not isinstance(artifact, dict)
            or artifact.get("schema_version") != 1
            or artifact.get("app_id") != spec["app_id"]
            or artifact.get("source_hash") != spec["source_hash"]
            or artifact.get("entrypoint") != spec["entrypoint"]
            or not IMAGE.fullmatch(artifact.get("image", ""))
            or not DIGEST.fullmatch(artifact.get("cache_key", ""))
        ):
            raise ValueError("invalid artifact")
    elif value["artifact"] is not None:
        raise ValueError("unexpected artifact")
    return value

"""Validate an offline snapshot without importing or running uploaded app code."""

import hashlib
import json
import re
import secrets
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from builder.policy import strict_json
from deployment.backup_archive import HASH, BackupError, regular
from deployment.cli import verify_certificate
from deployment.config import validate


def database(path, *, writable=False):
    connection = sqlite3.connect(path.as_uri() + ("?mode=rw" if writable else "?mode=ro"), uri=True)
    connection.execute("PRAGMA trusted_schema=OFF")
    connection.execute("PRAGMA foreign_keys=ON")
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('trigger','view') "
        "OR upper(sql) LIKE '%VIRTUAL TABLE%'"
    ).fetchone():
        connection.close()
        raise BackupError("Estrutura SQLite não permitida.")
    return connection


def inspect_database(path, key):
    with database(path) as db:
        if (
            db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]
            or db.execute("PRAGMA foreign_key_check").fetchone()
        ):
            raise BackupError("Banco de metadados inconsistente.")
        expected = {
            p.stem for p in (Path(__file__).parents[1] / "control/migrations").glob("0*.py")
        }
        actual = {
            r[0] for r in db.execute("SELECT name FROM django_migrations WHERE app='control'")
        }
        if expected != actual:
            raise BackupError("Migrações incompatíveis com esta versão instalada.")
        for query in (
            "SELECT 1 FROM control_application WHERE desired_state != 'stopped' "
            "OR active_publication_id IS NOT NULL",
            "SELECT 1 FROM control_appinstance WHERE enabled != 0",
            "SELECT 1 FROM control_job WHERE status IN ('queued','running')",
        ):
            if db.execute(query).fetchone():
                raise BackupError("Pare todos os apps e conclua as operações antes do backup.")
        cipher = AESGCM(key)
        for app, name, revision, raw in db.execute(
            "SELECT app_id,name,revision,ciphertext FROM control_environmentvalue"
        ):
            cipher.decrypt(raw[:12], raw[12:], f"{uuid.UUID(app)}:{name}:{revision}".encode())
        for identity, app, revision, raw in db.execute(
            "SELECT id,app_id,config_revision,environment_snapshot FROM control_publication "
            "WHERE config_revision != 0"
        ):
            cipher.decrypt(
                raw[:12],
                raw[12:],
                f"publication:{uuid.UUID(identity)}:{uuid.UUID(app)}:{revision}".encode(),
            )
        return list(
            db.execute(
                "SELECT app_id,source_path,source_hash,image_digest,build_manifest "
                "FROM control_version"
            )
        )


def inspect_snapshot(root):
    config = validate(strict_json((root / "config/control.json").read_bytes()))
    verify_certificate(
        (root / "config/cert.pem").read_bytes(),
        (root / "config/tls.key").read_bytes(),
        config,
        require_current=False,
    )
    key = (root / "control/keys/secrets.key").read_bytes()
    if (
        len(key) != 32
        or len((root / "control/keys/django.key").read_bytes()) < 50
        or not re.fullmatch(rb"[a-f0-9]{64}", (root / "control/keys/gateway.key").read_bytes())
    ):
        raise BackupError("Chaves da instalação ausentes ou inválidas.")
    versions = inspect_database(root / "control/metadata.sqlite3", key)
    runtime = strict_json((root / "config/runtime.json").read_bytes())
    builder = strict_json((root / "config/builder.json").read_bytes())
    if runtime.get("schema_version") != 1 or builder.get("schema_version") != 1:
        raise BackupError("Políticas de execução incompatíveis.")
    images = set(runtime["recipes"].values())
    images.update(v["image"] for v in builder["environments"].values())
    artifacts = {}
    for path in (root / "builder/artifacts").glob("*.json"):
        value = strict_json(path.read_bytes())
        if value.get("cache_key") != path.stem or value.get("schema_version") != 1:
            raise BackupError("Artefato de publicação inválido.")
        images.update([value["image"], value["base_image"]])
        artifacts[path.stem] = value
    for app, source, checksum, image, raw in versions:
        if not re.fullmatch(r"sources/[a-f0-9-]{36}\.zip", source):
            raise BackupError("Referência ao código inválida.")
        with regular(root / "control" / source) as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != checksum:
                raise BackupError("Código ausente ou diferente do registrado no banco.")
        if image:
            manifest = strict_json(raw)
            artifact = artifacts.get(manifest.get("cache_key"))
            if (
                artifact is None
                or artifact["image"] != image
                or artifact["app_id"] != str(uuid.UUID(app))
                or artifact["source_hash"] != checksum
            ):
                raise BackupError("Imagem preparada não corresponde ao código e ao app.")
            images.add(image)
    for path in (root / "runtime").glob("*.json"):
        record = strict_json(path.read_bytes())
        if record.get("desired") != "removed" or record.get("id") != path.stem:
            raise BackupError("Retire as instâncias do executor antes do backup.")
    if not images or any(not re.fullmatch("sha256:" + HASH, image) for image in images):
        raise BackupError("Referência de imagem inválida.")
    return sorted(images)


def reset_access(root, backup_id):
    """Preserve data/encryption identities; expire transport credentials and recovery consent."""
    with database(root / "control/metadata.sqlite3", writable=True) as db:
        for table in (
            "control_gatewayrequest",
            "control_gatewaysession",
            "control_gatewayticket",
            "django_session",
            "control_loginattempt",
        ):
            db.execute(f"DELETE FROM {table}")
        db.execute("UPDATE control_user SET session_revision=session_revision+1")
        db.execute("UPDATE control_appinstance SET enabled=0,draining_for_id=NULL")
        db.execute(
            "UPDATE control_recoverystate SET enabled=0,authorized_by_id=NULL,revision=revision+1,"
            "state='unobserved',attempts=0,halt_after_cleanup=0,source_id=NULL,job_id=NULL,"
            "next_attempt_at=NULL,healthy_since=NULL,checked_at=NULL,lease_token=NULL,"
            "message='Recuperação desabilitada após restauração.'"
        )
        db.execute(
            "INSERT INTO control_auditevent(actor_id,action,target,details,created_at) "
            "VALUES(NULL,?,?,?,?)",
            ("server.restored", backup_id, json.dumps({"apps_stopped": True}), datetime.now(UTC)),
        )
    # Old password-reset links, cookies and internal transport tokens must not survive a restore.
    (root / "control/keys/django.key").write_text(secrets.token_urlsafe(64))
    (root / "control/keys/gateway.key").write_text(secrets.token_hex(32))

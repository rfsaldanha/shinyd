import hashlib
import os
import uuid
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from .models import Application, AuditEvent, EnvironmentValue, Job, Version
from .permissions import can_create, manageable_apps


class Conflict(Exception):
    pass


def event(actor, action, target, **details):
    return AuditEvent.objects.create(
        actor=actor, action=action, target=str(target), details=details
    )


def require_publish(actor, app_id):
    app = manageable_apps(actor).filter(pk=app_id).first()
    if app is None:
        raise PermissionDenied
    return app


@transaction.atomic
def create_app(actor, values):
    if not can_create(actor):
        raise PermissionDenied
    app = Application(owner=actor, **values)
    app.full_clean()
    app.save()
    event(actor, "app.created", app.pk)
    return app


@transaction.atomic
def update_app(actor, app_id, expected_revision, values):
    require_publish(actor, app_id)
    app = Application.objects.get(pk=app_id)
    if app.revision != expected_revision:
        raise Conflict("O aplicativo mudou. Recarregue a página antes de salvar.")
    for key, value in values.items():
        if key not in {"name", "slug", "description", "visibility", "priority"}:
            raise ValidationError("Campo de configuração inválido.")
        setattr(app, key, value)
    app.revision += 1
    app.full_clean()
    app.save()
    event(actor, "app.updated", app.pk, fields=sorted(values))
    return app


def master_key():
    path = Path(settings.SHINYD_MASTER_KEY_FILE)
    if path.stat().st_mode & 0o077:
        raise ValidationError("Chave mestra com permissões inseguras; exija 0600.")
    key = path.read_bytes()
    if len(key) != 32:
        raise ValidationError("Chave mestra inválida.")
    return key


@transaction.atomic
def set_environment(actor, app_id, expected_revision, name, value, is_secret):
    require_publish(actor, app_id)
    # Validation is also enforced outside the HTML form boundary.
    from .forms import EnvironmentForm

    form = EnvironmentForm(
        {"revision": expected_revision, "name": name, "value": value, "is_secret": is_secret}
    )
    if not form.is_valid():
        raise ValidationError("Nome ou valor de ambiente inválido.")
    app = Application.objects.get(pk=app_id)
    if app.config_revision != expected_revision:
        raise Conflict("A configuração mudou. Recarregue antes de salvar.")
    app.config_revision += 1
    nonce = os.urandom(12)
    aad = f"{app.pk}:{name}:{app.config_revision}".encode()
    encrypted = AESGCM(master_key()).encrypt(nonce, value.encode(), aad)
    EnvironmentValue.objects.update_or_create(
        app=app,
        name=name,
        defaults={
            "is_secret": is_secret,
            "ciphertext": nonce + encrypted,
            "revision": app.config_revision,
        },
    )
    app.save(update_fields=["config_revision", "updated_at"])
    event(actor, "environment.updated", app.pk, name=name, revision=app.config_revision)


def decrypt_environment(value):
    """Internal runtime boundary only; never expose from a view or export."""
    raw = bytes(value.ciphertext)
    aad = f"{value.app_id}:{value.name}:{value.revision}".encode()
    return AESGCM(master_key()).decrypt(raw[:12], raw[12:], aad).decode()


def receive_upload(actor, app_id, upload, idempotency_key, entrypoint=""):
    app = require_publish(actor, app_id)
    if upload.size is not None and upload.size > settings.SHINYD_UPLOAD_LIMIT:
        raise ValidationError("ZIP excede o tamanho máximo permitido.")
    root = settings.STATE_DIR / "sources"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    source_id = uuid.uuid4()
    path = root / f"{source_id}.zip"
    keep = False
    try:
        digest = hashlib.sha256()
        total = 0
        with path.open("xb") as file:
            os.chmod(path, 0o600)
            for chunk in upload.chunks():
                total += len(chunk)
                if total > settings.SHINYD_UPLOAD_LIMIT:
                    raise ValidationError("ZIP excede o tamanho máximo permitido.")
                digest.update(chunk)
                file.write(chunk)
            file.flush()
            os.fsync(file.fileno())
        if not total:
            raise ValidationError("O arquivo está vazio.")
        with transaction.atomic():
            # Authorization can have changed during a long upload.
            require_publish(actor, app_id)
            existing = Job.objects.filter(idempotency_key=idempotency_key).first()
            if existing:
                if (
                    existing.app_id != app.id
                    or existing.actor_id != actor.id
                    or existing.version.source_hash != digest.hexdigest()
                    or existing.version.entrypoint != entrypoint
                ):
                    raise Conflict("Identificador de envio já utilizado para outro conteúdo.")
                return existing
            if (
                Job.objects.filter(status__in=[Job.Status.QUEUED, Job.Status.RUNNING]).count()
                >= 100
            ):
                raise ValidationError("Fila de publicações cheia. Tente novamente mais tarde.")
            version = Version.objects.create(
                app=app,
                created_by=actor,
                source_hash=digest.hexdigest(),
                source_path=f"sources/{source_id}.zip",
                entrypoint=entrypoint,
            )
            job = Job.objects.create(
                app=app,
                version=version,
                actor=actor,
                kind="inspect_upload",
                idempotency_key=idempotency_key,
            )
            event(actor, "upload.received", app.pk, version=str(version.pk), job=str(job.pk))
        keep = True
        return job
    finally:
        if not keep:
            path.unlink(missing_ok=True)

import tempfile
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .archives import ArchiveError, inspect_archive
from .models import Job, Version


@transaction.atomic
def claim():
    # This kind is read-only and safe to retry. Runtime commands will require
    # separate reconciliation; do not extend this retry rule to mutations.
    expired = Job.objects.filter(
        kind="inspect_upload", status=Job.Status.RUNNING, lease_until__lt=timezone.now()
    )
    expired.filter(attempts__gte=3).update(
        status=Job.Status.FAILED,
        phase="interrupted",
        message="A inspeção foi interrompida três vezes. Reenvie após verificar o worker.",
        lease_token=None,
        lease_until=None,
    )
    expired.filter(attempts__lt=3).update(
        status=Job.Status.QUEUED, lease_token=None, lease_until=None
    )
    job = (
        Job.objects.filter(kind="inspect_upload", status=Job.Status.QUEUED)
        .order_by("created_at", "id")
        .first()
    )
    if job is None:
        return None
    if job.cancel_requested:
        job.status = Job.Status.CANCELLED
        job.save(update_fields=["status", "updated_at"])
        return None
    token = uuid.uuid4()
    Job.objects.filter(pk=job.pk, status=Job.Status.QUEUED).update(
        status=Job.Status.RUNNING,
        phase="validating",
        attempts=F("attempts") + 1,
        lease_token=token,
        lease_until=timezone.now() + timedelta(minutes=5),
    )
    return Job.objects.get(pk=job.pk)


@transaction.atomic
def finish(job, *, manifest=None, error=None):
    current = Job.objects.get(pk=job.pk)
    if current.status != Job.Status.RUNNING or current.lease_token != job.lease_token:
        return False  # Old worker cannot overwrite the result of a newer lease.
    if current.cancel_requested:
        current.status, current.phase = Job.Status.CANCELLED, "cancelled"
        current.message = "Inspeção cancelada."
        Version.objects.filter(pk=current.version_id).update(status="cancelled")
    elif error:
        current.status, current.phase = Job.Status.FAILED, "validation_failed"
        current.message = error
        Version.objects.filter(pk=current.version_id).update(status="invalid")
    else:
        current.status, current.phase = Job.Status.SUCCEEDED, "validated"
        current.message = (
            "ZIP validado. Nenhum código foi executado. "
            "Build e publicação aguardam a homologação do executor."
        )
        Version.objects.filter(pk=current.version_id).update(
            status="validated",
            entrypoint=manifest["entrypoint"],
            manifest=manifest,
            r_version=manifest["r_version"],
        )
    current.lease_token, current.lease_until = None, None
    current.save()
    return True


def process_one():
    job = claim()
    if job is None:
        return False
    staging = settings.STATE_DIR / "staging"
    staging.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        source = (settings.STATE_DIR / job.version.source_path).resolve()
        if not source.is_relative_to((settings.STATE_DIR / "sources").resolve()):
            raise ArchiveError("Referência interna de arquivo inválida.")
        with tempfile.TemporaryDirectory(dir=staging, prefix="inspect-") as directory:
            from pathlib import Path

            result = inspect_archive(
                source,
                Path(directory),
                max_bytes=settings.SHINYD_EXTRACT_LIMIT,
                max_entries=settings.SHINYD_ZIP_ENTRIES,
                entrypoint=job.version.entrypoint,
                cancelled=lambda: (
                    not Job.objects.filter(
                        pk=job.pk,
                        status=Job.Status.RUNNING,
                        lease_token=job.lease_token,
                        cancel_requested=False,
                    ).exists()
                ),
            )
        if result["source_hash"] != job.version.source_hash:
            raise ArchiveError("O arquivo armazenado não corresponde ao hash do envio.")
        finish(job, manifest=result)
    except ArchiveError as exc:
        finish(job, error=str(exc))
    except OSError:
        finish(job, error="Falha de armazenamento. Verifique espaço e permissões do worker.")
    except Exception:
        # No exception value: uploaded files can put sensitive data in diagnostics.
        finish(job, error="Falha interna de inspeção. Consulte o administrador.")
        raise
    return True

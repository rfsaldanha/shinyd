"""Reconcile durable builds; a lost worker lease never invents another build."""

import uuid
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from builder.policy import MESSAGES, SLUG
from builder.protocol import PHASES

from . import builder_client
from .models import Job, Version
from .permissions import manageable_apps
from .services import Conflict, event, require_publish


@transaction.atomic
def enqueue(actor, version_id, environment, key):
    version = Version.objects.select_related("app").get(pk=version_id)
    require_publish(actor, version.app_id)
    if not isinstance(environment, str) or not SLUG.fullmatch(environment):
        raise ValidationError("Ambiente inválido.")
    existing = Job.objects.filter(idempotency_key=key).first()
    if existing:
        if (
            existing.kind != "prepare_version"
            or existing.actor_id != actor.pk
            or existing.version_id != version.pk
            or existing.build_environment != environment
        ):
            raise Conflict("Identificador já utilizado para outra preparação.")
        return existing
    if version.status not in {"validated", "build_failed", "build_cancelled"}:
        raise Conflict("Esta versão não está disponível para preparação.")
    if Job.objects.filter(version=version, status__in=["queued", "running"]).exists():
        raise Conflict("Esta versão já tem uma operação em andamento.")
    if Job.objects.filter(status__in=["queued", "running"]).count() >= 100:
        raise ValidationError("Fila cheia. Aguarde a conclusão dos trabalhos anteriores.")
    job = Job.objects.create(
        app=version.app,
        version=version,
        actor=actor,
        kind="prepare_version",
        build_environment=environment,
        idempotency_key=key,
        message="Na fila de preparação. A publicação atual será preservada.",
    )
    version.status = "build_queued"
    version.save(update_fields=["status"])
    event(actor, "build.queued", job.pk, version=str(version.pk), environment=environment)
    return job


@transaction.atomic
def claim():
    # One non-terminal build is reconciled before the next queued one. Existing
    # uploads can still be inspected while this build runs outside the worker.
    active = (
        Job.objects.filter(kind="prepare_version", status="running").order_by("created_at").first()
    )
    if active and active.lease_until and active.lease_until > timezone.now():
        return None
    job = active or Job.objects.filter(kind="prepare_version", status="queued").first()
    if not job:
        return None
    first = job.status == "queued"
    job.status, job.lease_token = "running", uuid.uuid4()
    job.lease_until = timezone.now() + timedelta(seconds=90)
    job.attempts += int(first)
    job.save()
    return Job.objects.select_related("version", "actor").get(pk=job.pk)


@transaction.atomic
def record(job, result=None, *, error=None):
    current = Job.objects.get(pk=job.pk)
    if current.status != "running" or current.lease_token != job.lease_token:
        return False
    if result and result["status"] in {"succeeded", "failed", "cancelled"}:
        cancelled = (
            current.cancel_requested
            or not manageable_apps(current.actor).filter(pk=current.app_id).exists()
        )
        cleanup_failed = result["error"] == "cleanup"
        status = "cancelled" if cancelled and not cleanup_failed else result["status"]
        current.status = status
        current.phase = "prepared" if status == "succeeded" else status
        current.message = PHASES[current.phase]
        if result["error"]:
            current.message = MESSAGES.get(result["error"], MESSAGES["storage"])
        if status == "succeeded":
            Version.objects.filter(pk=current.version_id).update(
                status="prepared",
                image_digest=result["artifact"]["image"],
                build_manifest=result["artifact"],
            )
        else:
            Version.objects.filter(pk=current.version_id).update(
                status="build_cancelled" if status == "cancelled" else "build_failed",
            )
        event(current.actor, "build." + status, current.pk, code=result["error"])
    elif result:
        current.phase = result["phase"]
        current.message = PHASES[result["phase"]]
    else:
        current.phase = "waiting_builder"
        current.message = error or "Aguardando disponibilidade do preparador."
    if current.status == "running" and current.cancel_requested:
        current.message = "Cancelamento solicitado; aguardando confirmação e limpeza do preparador."
    current.lease_token = None
    current.lease_until = (
        timezone.now() + timedelta(seconds=3) if current.status == "running" else None
    )
    current.save()
    return True


def process_one():
    job = claim()
    if not job:
        return False
    try:
        # Recheck permission after a queue wait. Revocation cancels even an
        # accepted build; it cannot grant new permissions by completing later.
        if not manageable_apps(job.actor).filter(pk=job.app_id).exists():
            Job.objects.filter(pk=job.pk).update(cancel_requested=True)
        current = Job.objects.get(pk=job.pk)
        if current.cancel_requested:
            result = builder_client.cancel(job)
        else:
            result = builder_client.inspect(job)
            if result is None:
                result = builder_client.submit(job)
        record(job, result)
    except builder_client.BuilderRejected as exc:
        if exc.code in {"busy", "admission"}:
            record(job, error=str(exc))
        else:
            record(
                job,
                {
                    "status": "failed",
                    "error": exc.code,
                    "artifact": None,
                },
            )
    except builder_client.BuilderUnavailable:
        record(
            job,
            error="Preparador indisponível. A consulta será retomada com o mesmo identificador.",
        )
    except OSError:
        record(job, {"status": "failed", "error": "storage", "artifact": None})
    return True

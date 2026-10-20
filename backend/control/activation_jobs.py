"""Once-only candidates, atomic route switch, durable retirement after cancellation."""

import uuid
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from builder.policy import IMAGE
from builder.protocol import DIGEST

from . import runtime_client
from .gateway_auth import instance_origin
from .models import AppInstance, Application, Job, Publication, Version
from .permissions import manageable_apps
from .services import Conflict, event, require_publish


def eligible(app):
    if not settings.SHINYD_RUNTIME_ENABLED:
        raise ValidationError("Ativação desabilitada. Consulte o administrador.")
    if app.environment.exists():
        raise ValidationError(
            "A aplicação de variáveis e segredos ao processo ainda não está disponível. "
            "A ativação de apps com esses valores configurados está bloqueada."
        )


@transaction.atomic
def enqueue(actor, version_id, key):
    version = Version.objects.select_related("app__priority").get(pk=version_id)
    app = require_publish(actor, version.app_id)
    existing = Job.objects.filter(idempotency_key=key).first()
    if existing:
        if (
            existing.kind != "activate_version"
            or existing.actor_id != actor.pk
            or existing.version_id != version.pk
        ):
            raise Conflict("Identificador já utilizado para outra operação.")
        return existing
    eligible(app)
    artifact = version.build_manifest
    if (
        version.status != "prepared"
        or not isinstance(artifact, dict)
        or artifact.get("app_id") != str(app.pk)
        or artifact.get("source_hash") != version.source_hash
        or artifact.get("image") != version.image_digest
        or not IMAGE.fullmatch(version.image_digest)
        or not isinstance(artifact.get("cache_key"), str)
        or not DIGEST.fullmatch(artifact["cache_key"])
    ):
        raise ValidationError("Esta versão não possui artefato preparado válido.")
    if Job.objects.filter(
        app=app, kind="activate_version", status__in=["queued", "running"]
    ).exists():
        raise Conflict("Este app já tem uma ativação ou limpeza em andamento.")
    if Job.objects.filter(status__in=["queued", "running"]).count() >= 100:
        raise ValidationError("Fila cheia. Aguarde a conclusão dos trabalhos anteriores.")
    if app.active_publication_id and app.active_publication.version_id == version.pk:
        raise Conflict("Esta versão já está ativa.")
    publication = Publication.objects.create(
        app=app,
        version=version,
        created_by=actor,
        config_revision=app.config_revision,
        app_revision=app.revision,
        previous_id=app.active_publication_id,
        runtime_spec={
            "app_id": str(app.pk),
            "recipe": "",
            "priority": app.priority.slug,
            "artifact": artifact["cache_key"],
            "source_hash": version.source_hash,
        },
    )
    instance = AppInstance(
        id=publication.pk,
        app=app,
        publication=publication,
        host=f"p{publication.pk.hex}.{settings.SHINYD_APP_DOMAIN}",
        target="",
        enabled=False,
    )
    if not settings.DEBUG and settings.SHINYD_HTTPS_PORT != 443:
        instance.host += f":{settings.SHINYD_HTTPS_PORT}"
    try:
        if len(instance.host) > 259:
            raise ValueError
        instance_origin(instance)
    except ValueError as exc:
        raise ValidationError("Domínio ou porta dos aplicativos inválidos.") from exc
    instance.save()
    job = Job.objects.create(
        app=app,
        version=version,
        publication=publication,
        actor=actor,
        kind="activate_version",
        idempotency_key=key,
        message="Na fila de ativação. A publicação anterior será preservada até a troca.",
    )
    event(actor, "activation.queued", job.pk, publication=str(publication.pk))
    return job


@transaction.atomic
def claim():
    job = (
        Job.objects.filter(kind="activate_version", status__in=["queued", "running"])
        .filter(Q(lease_until__isnull=True) | Q(lease_until__lte=timezone.now()))
        .first()
    )
    if not job:
        return None
    job.attempts += int(job.status == "queued")
    job.status, job.lease_token = "running", uuid.uuid4()
    job.lease_until = timezone.now() + timedelta(seconds=90)
    job.save()
    return job


def current(job):
    return (
        Job.objects.select_related("publication__version", "publication__previous", "actor", "app")
        .filter(
            pk=job.pk,
            status="running",
            lease_token=job.lease_token,
        )
        .first()
    )


@transaction.atomic
def update(job, *, phase, message, status="running"):
    value = current(job)
    if not value:
        return False
    if status == "running" and value.publication.status in {"failed", "cancelled"}:
        # Preserve the reason for aborting while waiting for confirmed cleanup.
        phase, message = "cleaning_candidate", value.message
    value.phase, value.message, value.status = phase, message, status
    value.lease_token = None
    value.lease_until = timezone.now() + timedelta(seconds=5) if status == "running" else None
    value.save()
    if status != "running":
        event(value.actor, "activation." + status, value.pk)
    return True


def invalidated(job):
    pub, app = job.publication, job.app
    if job.cancel_requested or not manageable_apps(job.actor).filter(pk=app.pk).exists():
        return "cancelled", "Ativação cancelada. A publicação anterior foi preservada."
    if timezone.now() > job.created_at + timedelta(minutes=15):
        return "failed", "Prazo de ativação excedido. A publicação anterior foi preservada."
    if (
        not settings.SHINYD_RUNTIME_ENABLED
        or app.environment.exists()
        or app.revision != pub.app_revision
        or app.config_revision != pub.config_revision
        or app.active_publication_id != pub.previous_id
        or app.priority.slug != pub.runtime_spec["priority"]
    ):
        return "failed", "Configuração alterada durante a ativação. Solicite novamente."
    return None


@transaction.atomic
def decide(job, result=None, failure=None):
    value = current(job)
    if not value or value.publication.status != "candidate":
        return False
    failure = invalidated(value) or failure
    pub = value.publication
    if failure:
        pub.status, value.message = failure
        value.phase = "cleaning_candidate"
    elif result:
        if result.get("image") != pub.version.image_digest:
            pub.status, value.phase = "failed", "cleaning_candidate"
            value.message = "O executor retornou uma imagem diferente da versão preparada."
        else:
            # Route and pointer change in the same transaction. Existing hosts
            # never change target, so a cookie/WS cannot drift to another R process.
            AppInstance.objects.filter(app=value.app, enabled=True).update(enabled=False)
            AppInstance.objects.filter(publication=pub).update(
                enabled=True, target=result["target"]
            )
            Application.objects.filter(pk=value.app_id).update(
                active_publication=pub, desired_state="running"
            )
            if pub.previous_id:
                Publication.objects.filter(pk=pub.previous_id).update(status="retiring")
            pub.status, value.phase = "active", "retiring_previous"
            value.message = "Versão ativada. Encerrando a instância anterior."
            event(value.actor, "publication.activated", pub.pk, version=str(pub.version_id))
    pub.save(update_fields=["status"])
    value.save(update_fields=["phase", "message", "updated_at"])
    return True


def process_one():
    job = claim()
    if not job:
        return False
    value = current(job)
    pub = value.publication
    try:
        if pub.status == "candidate":
            failure = invalidated(value)
            if failure:
                decide(job, failure=failure)
            else:
                result = runtime_client.inspect(pub)
                if result is None:
                    result = runtime_client.ensure(pub)
                if result["state"] == "ready":
                    decide(job, result=result)
                elif result["state"] in {"failed", "stopped", "removed"}:
                    code = result.get("error") or "application_readiness_failed"
                    decide(
                        job,
                        failure=(
                            "failed",
                            runtime_client.MESSAGES.get(code, "Falha na partida do aplicativo."),
                        ),
                    )
                else:
                    update(
                        job, phase="starting", message="Aguardando a verificação de funcionamento."
                    )
                    return True
        value = current(job)
        if not value:
            return True
        pub = value.publication
        if pub.status in {"failed", "cancelled"}:
            runtime_client.retire(pub)
            update(job, phase=pub.status, message=value.message, status=pub.status)
        elif pub.status == "active":
            if pub.previous_id:
                runtime_client.retire(pub.previous)
                Publication.objects.filter(pk=pub.previous_id).update(status="retired")
            update(
                job,
                phase="active",
                message="Versão ativada. Abra o aplicativo pela página do app.",
                status="succeeded",
            )
    except runtime_client.RuntimeRejected as exc:
        value = current(job)
        if (
            value
            and value.publication.status == "candidate"
            and exc.code not in {"runtime_busy", "admission_denied"}
        ):
            decide(job, failure=("failed", str(exc)))
        update(job, phase="waiting_runtime", message=str(exc))
    except runtime_client.RuntimeUnavailable:
        update(
            job,
            phase="waiting_runtime",
            message="Executor indisponível. A operação será retomada com o mesmo identificador; "
            "pode haver limpeza pendente.",
        )
    return True

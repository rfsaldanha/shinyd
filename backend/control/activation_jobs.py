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
from .environment import snapshot
from .gateway_auth import instance_origin
from .models import AppInstance, Application, Job, Publication, Version
from .permissions import manageable_apps
from .services import Conflict, event, require_publish


def eligible(app):
    if not settings.SHINYD_RUNTIME_ENABLED:
        raise ValidationError("Ativação desabilitada. Consulte o administrador.")


@transaction.atomic
def enqueue(actor, version_id, key, *, restored_from=None, expected_state=None):
    version = Version.objects.select_related("app__priority").get(pk=version_id)
    app = require_publish(actor, version.app_id)
    existing = Job.objects.filter(idempotency_key=key).first()
    if existing:
        if (
            existing.kind != "activate_version"
            or existing.actor_id != actor.pk
            or existing.version_id != version.pk
            or not existing.publication_id
            or existing.publication.restored_from_id != restored_from
        ):
            raise Conflict("Identificador já utilizado para outra operação.")
        return existing
    if restored_from is not None:
        source = Publication.objects.filter(
            pk=restored_from, app=app, version=version, status="retired"
        ).first()
        if source is None:
            raise ValidationError("Escolha uma publicação anterior encerrada deste app.")
        if expected_state != rollback_state(app, source):
            raise Conflict("O aplicativo ou sua configuração mudou. Reabra a confirmação.")
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
    if (
        app.active_publication_id
        and app.active_publication.version_id == version.pk
        and app.active_publication.config_revision == app.config_revision
        and bool(app.active_publication.runtime_spec.get("data_mode")) == app.data_enabled
    ):
        raise Conflict("Esta versão e revisão de configuração já estão ativas.")
    publication = Publication(
        app=app,
        version=version,
        created_by=actor,
        config_revision=app.config_revision,
        app_revision=app.revision,
        previous_id=app.active_publication_id,
        restored_from_id=restored_from,
        runtime_spec={
            "app_id": str(app.pk),
            "recipe": "",
            "priority": app.priority.slug,
            "artifact": artifact["cache_key"],
            "source_hash": version.source_hash,
            **({"config_revision": app.config_revision} if app.config_revision else {}),
            **({"data_mode": "persistent"} if app.data_enabled else {}),
        },
    )
    publication.environment_snapshot = snapshot(publication)
    publication.save()
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
        message="Na fila de ativação. A candidata será verificada antes da troca.",
    )
    event(actor, "activation.queued", job.pk, publication=str(publication.pk))
    if restored_from:
        event(
            actor,
            "rollback.queued",
            job.pk,
            publication=str(publication.pk),
            restored_from=str(restored_from),
            version=str(version.pk),
            config_revision=app.config_revision,
        )
    return job


def rollback_state(app, source):
    """Confirmation binds code and current settings, never historical secret values."""
    return {
        "app": str(app.pk),
        "source": str(source.pk),
        "version": str(source.version_id),
        "revision": app.revision,
        "config_revision": app.config_revision,
        "active": str(app.active_publication_id) if app.active_publication_id else None,
    }


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
        or app.revision != pub.app_revision
        or app.config_revision != pub.config_revision
        or app.active_publication_id != (None if pub.data_cutover else pub.previous_id)
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
    if result and result.get("image") != pub.version.image_digest:
        failure = failure or (
            "failed",
            "O executor retornou uma imagem diferente da versão preparada.",
        )
    if result and not failure and pub.runtime_spec.get("data_mode") == "persistent":
        if (
            not pub.data_preflight_passed
            or not pub.data_cutover
            or (pub.previous_id and pub.previous.status != "retired")
        ):
            return False
    if failure:
        pub.status, value.message = failure
        if pub.data_cutover:
            value.message = (
                "Ativação interrompida após desligar a publicação anterior. "
                "O volume foi mantido, mas o app pode ter alterado seus dados. "
                "Corrija a causa e ative uma versão novamente. "
                + failure[1].replace(" A publicação anterior foi preservada.", "")
            )
        value.phase = "cleaning_candidate"
    elif result:
        # Route and pointer change in the same transaction. Existing hosts
        # never change target, so a cookie/WS cannot drift to another R process.
        AppInstance.objects.filter(app=value.app, enabled=True).update(enabled=False)
        AppInstance.objects.filter(publication=pub).update(enabled=True, target=result["target"])
        Application.objects.filter(pk=value.app_id).update(
            active_publication=pub, desired_state="running"
        )
        if pub.previous_id and pub.previous.status != "retired":
            Publication.objects.filter(pk=pub.previous_id).update(status="retiring")
        pub.status, value.phase = "active", "retiring_previous"
        value.message = "Versão ativada. Finalizando a troca."
        event(
            value.actor,
            "publication.activated",
            pub.pk,
            version=str(pub.version_id),
            **({"restored_from": str(pub.restored_from_id)} if pub.restored_from_id else {}),
        )
    pub.save(update_fields=["status"])
    value.save(update_fields=["phase", "message", "updated_at"])
    return True


@transaction.atomic
def data_checkpoint(job, *, preflight=False, cutover=False):
    """Durable checkpoints before discarding the probe or stopping the writer."""
    value = current(job)
    if (
        not value
        or value.publication.status != "candidate"
        or value.publication.runtime_spec.get("data_mode") != "persistent"
    ):
        return False
    if failure := invalidated(value):
        decide(job, failure=failure)
        return False
    pub = value.publication
    if preflight:
        pub.data_preflight_passed = True
        pub.save(update_fields=["data_preflight_passed"])
    if cutover:
        if not pub.data_preflight_passed:
            return False
        AppInstance.objects.filter(app=value.app, enabled=True).update(enabled=False)
        Application.objects.filter(pk=value.app_id).update(
            active_publication=None, desired_state="stopped"
        )
        if pub.previous_id:
            Publication.objects.filter(pk=pub.previous_id).update(status="retiring")
        pub.data_cutover = True
        pub.save(update_fields=["data_cutover"])
        event(value.actor, "data.cutover", pub.pk)
    return True


def prepare_persistent(job, value):
    """One bounded stage per lease. Never expose live data to a concurrent probe."""
    pub = value.publication
    if not pub.data_preflight_passed:
        result = runtime_client.inspect(pub, probe=True)
        if result is None:
            result = runtime_client.ensure(pub, probe=True)
        if result["state"] == "ready" and result.get("image") == pub.version.image_digest:
            data_checkpoint(job, preflight=True)
        elif result["state"] in {"ready", "failed", "stopped", "removed"}:
            decide(job, failure=("failed", "A candidata falhou no teste com dados temporários."))
        update(
            job, phase="data_preflight", message="Verificando a candidata com dados temporários."
        )
        return False
    if not pub.data_cutover:
        runtime_client.retire(pub, probe=True)
        data_checkpoint(job, cutover=True)
        update(
            job,
            phase="data_cutover",
            message="Interrompendo a publicação anterior para liberar os dados.",
        )
        return False
    if pub.previous_id and pub.previous.status != "retired":
        runtime_client.retire(pub.previous)
        Publication.objects.filter(pk=pub.previous_id).update(status="retired")
        update(
            job,
            phase="data_starting",
            message="Instância anterior removida. Preparando a partida com dados persistentes.",
        )
        return False
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
                if pub.runtime_spec.get("data_mode") == "persistent" and not prepare_persistent(
                    job, value
                ):
                    return True
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
            if pub.runtime_spec.get("data_mode") == "persistent":
                runtime_client.retire(pub, probe=True)
                if pub.data_cutover and pub.previous_id:
                    runtime_client.retire(pub.previous)
                    Publication.objects.filter(pk=pub.previous_id).update(status="retired")
            runtime_client.retire(pub)
            update(job, phase=pub.status, message=value.message, status=pub.status)
        elif pub.status == "active":
            if pub.previous_id and pub.previous.status != "retired":
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

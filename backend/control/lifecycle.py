"""Bounded batches with frozen targets and a durable stop commit point."""

import uuid
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from . import activation_jobs, connections, runtime_client
from .models import (
    AppInstance,
    Application,
    GatewayRequest,
    Job,
    OperationBatch,
    OperationItem,
    Publication,
)
from .permissions import manageable_apps
from .services import Conflict, event, require_publish


def state(app):
    return {
        "app": str(app.pk),
        "revision": app.revision,
        "config_revision": app.config_revision,
        "active": str(app.active_publication_id) if app.active_publication_id else None,
        "version": str(app.last_version_id) if app.last_version_id else None,
        "desired": app.desired_state,
    }


@transaction.atomic
def enqueue(actor, action, mode, key, targets):
    if action not in OperationBatch.Action.values or mode not in Publication.SwitchMode.values:
        raise ValidationError("Operação inválida.")
    if not 1 <= len(targets) <= 100 or len({s["app"] for s in targets}) != len(targets):
        raise ValidationError("Escolha de um a cem aplicativos diferentes por operação.")
    existing = OperationBatch.objects.filter(idempotency_key=key).first()
    if existing:
        if (
            existing.actor_id != actor.pk
            or existing.action != action
            or existing.switch_mode != mode
            or existing.request_state != targets
        ):
            raise Conflict("Identificador já utilizado para outra operação.")
        return existing
    batch = OperationBatch.objects.create(
        actor=actor, action=action, switch_mode=mode, idempotency_key=key, request_state=targets
    )
    for target in targets:
        # The signed selection contained real apps; deleted or revoked targets disclose nothing.
        if not Application.objects.filter(pk=target["app"]).exists():
            continue
        item = OperationItem.objects.create(
            batch=batch, app_id=target["app"], expected_state=target
        )
        try:
            with transaction.atomic():
                app = require_publish(actor, item.app_id)
                if state(app) != target:
                    raise Conflict("O app ou sua configuração mudou. Confirme novamente.")
                activation_jobs.eligible(app)
                if Job.objects.filter(
                    app=app,
                    kind__in=["activate_version", "stop_app"],
                    status__in=["queued", "running"],
                ).exists():
                    raise Conflict("Aguarde a operação ou limpeza em andamento neste app.")
                if (action == "start" and app.active_publication_id) or (
                    action == "stop" and not app.active_publication_id
                ):
                    item.status = "succeeded"
                    item.message = "App já iniciado." if action == "start" else "App já parado."
                else:
                    if Job.objects.filter(status__in=["queued", "running"]).count() >= 100:
                        raise ValidationError("Fila cheia. Aguarde e solicite novamente este app.")
                    child_key = uuid.uuid5(batch.pk, str(app.pk))
                    if action == "stop":
                        item.job = Job.objects.create(
                            app=app,
                            publication=app.active_publication,
                            actor=actor,
                            kind="stop_app",
                            operation=action,
                            idempotency_key=child_key,
                            message="Parada na fila. A publicação continua disponível "
                            "até a execução.",
                        )
                    else:
                        if action == "restart" and not app.active_publication_id:
                            raise ValidationError("App parado. Use Iniciar ou ative uma versão.")
                        version = (
                            app.active_publication.version_id
                            if action == "restart"
                            else app.last_version_id
                        )
                        if not version:
                            raise ValidationError(
                                "Nenhuma versão publicada. Prepare e ative uma versão primeiro."
                            )
                        item.job = activation_jobs.enqueue(
                            actor, version, child_key, switch_mode=mode, operation=action
                        )
        except PermissionDenied:
            item.status, item.message = "failed", "Permissão de publicação indisponível."
        except (Conflict, ValidationError) as exc:
            item.status = "failed"
            item.message = " ".join(exc.messages) if isinstance(exc, ValidationError) else str(exc)
        item.save()
    event(actor, "operations.queued", batch.pk, operation=action, mode=mode, count=len(targets))
    return batch


def current(job):
    return activation_jobs.current(job)


def finish(value, status, message):
    """Caller holds the decision transaction and has checked its lease."""
    value.status, value.phase, value.message = status, status, message
    value.lease_token = value.lease_until = None
    value.save()
    event(value.actor, "stop." + status, value.pk)


@transaction.atomic
def checkpoint(job):
    value = current(job)
    if not value:
        return False
    item = value.operation_item
    if item.committed:
        return True  # Revocation/cancellation cannot abandon a committed removal.
    failure = None
    if value.cancel_requested or not manageable_apps(value.actor).filter(pk=value.app_id).exists():
        failure = ("cancelled", "Parada cancelada. A publicação foi preservada.")
    elif not settings.SHINYD_RUNTIME_ENABLED or state(value.app) != item.expected_state:
        failure = ("failed", "Configuração alterada. A parada foi cancelada; confirme novamente.")
    elif item.drain_started_at and timezone.now() >= item.drain_started_at + timedelta(minutes=5):
        failure = ("failed", "Espera de cinco minutos encerrada. A publicação foi preservada.")
    elif timezone.now() >= value.created_at + timedelta(minutes=15):
        failure = ("failed", "Prazo de parada excedido. A publicação foi preservada.")
    if not failure and item.batch.switch_mode == "drain":
        if not item.drain_started_at:
            item.drain_started_at = timezone.now()
            item.save(update_fields=["drain_started_at"])
            AppInstance.objects.filter(publication=value.publication, enabled=True).update(
                draining_for=value.publication
            )
            event(value.actor, "stop.drain_started", value.pk)
        activity = connections.counts(value.publication_id)
        if activity["uncertain"]:
            failure = (
                "failed",
                "Não foi possível confirmar as conexões. A publicação foi preservada.",
            )
        elif activity["total"]:
            value.phase = "waiting_connections"
            value.message = (
                "Aguardando o encerramento das conexões por até cinco minutos, sem forçar a parada."
            )
            value.lease_token = None
            value.lease_until = timezone.now() + timedelta(seconds=5)
            value.save()
            return False
    if failure:
        AppInstance.objects.filter(draining_for=value.publication).update(draining_for=None)
        finish(value, *failure)
        return False
    # Admission and routing use the same SQLite IMMEDIATE write transaction.
    AppInstance.objects.filter(app=value.app, enabled=True).update(enabled=False, draining_for=None)
    Application.objects.filter(pk=value.app_id).update(
        active_publication=None,
        last_version_id=value.publication.version_id,
        desired_state="stopped",
    )
    Publication.objects.filter(pk=value.publication_id).update(status="retiring")
    item.committed = True
    item.save(update_fields=["committed"])
    value.phase, value.message = (
        "stopping",
        "Acesso encerrado. Aguardando a remoção confirmada da instância.",
    )
    value.save(update_fields=["phase", "message", "updated_at"])
    event(value.actor, "stop.committed", value.pk, publication=str(value.publication_id))
    return True


@transaction.atomic
def complete(job):
    value = current(job)
    if value and value.operation_item.committed:
        Publication.objects.filter(pk=value.publication_id).update(status="retired")
        GatewayRequest.objects.filter(instance_id=value.publication_id).delete()
        finish(value, "succeeded", "App parado. Dados persistentes e configuração foram mantidos.")


@transaction.atomic
def retry(job):
    value = current(job)
    if value:
        value.phase = "stopping"
        value.message = (
            "Acesso encerrado; remoção da instância ainda não confirmada. "
            "O worker tentará novamente. Iniciar permanece bloqueado até concluir a limpeza."
        )
        value.lease_token = None
        value.lease_until = timezone.now() + timedelta(seconds=5)
        value.save()


def process_one():
    job = activation_jobs.claim("stop_app")
    if not job:
        return False
    if checkpoint(job):
        try:
            runtime_client.retire(job.publication)
            complete(job)
        except (runtime_client.RuntimeUnavailable, runtime_client.RuntimeRejected):
            retry(job)
    return True

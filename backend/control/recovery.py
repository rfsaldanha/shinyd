"""Opt-in recovery after proven process exit, with durable admission and a circuit."""

import uuid
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from . import activation_jobs, runtime_client
from .models import AppInstance, Application, Job, RecoveryState
from .permissions import manageable_apps
from .services import Conflict, event, require_publish

DELAYS = (10, 30, 60)
WINDOW = timedelta(minutes=10)
PENDING = {"queued", "running"}


def busy(app):
    return Job.objects.filter(
        app=app, kind__in=["activate_version", "stop_app"], status__in=PENDING
    ).exists()


def manual_request(app):
    """Inside the manual operation's transaction, after admission succeeds."""
    row, _ = RecoveryState.objects.get_or_create(app=app)
    row.state, row.message = "unobserved", "Operação manual solicitada."
    row.attempts = 0
    row.halt_after_cleanup = False
    row.source = row.job = None
    row.next_attempt_at = row.healthy_since = row.lease_token = None
    row.next_check_at = timezone.now()
    row.save()


@transaction.atomic
def configure(actor, app_id, revision, enabled):
    app = require_publish(actor, app_id)
    row, _ = RecoveryState.objects.get_or_create(app=app)
    if row.revision != revision:
        raise Conflict("A política mudou. Reabra a configuração.")
    if enabled and busy(app):
        raise Conflict("Aguarde a operação ou limpeza em andamento antes de habilitar.")
    if enabled:
        manual_request(app)
        row.refresh_from_db()
    elif row.job_id:
        Job.objects.filter(pk=row.job_id, status__in=PENDING).exclude(
            publication__status="active"
        ).update(cancel_requested=True)
    row.enabled, row.authorized_by = enabled, actor
    row.revision += 1
    row.lease_token = None
    row.next_check_at = timezone.now()
    if not enabled:
        row.next_attempt_at = None
        row.state = "recovering" if row.job_id else "unobserved"
        row.message = "Recuperação desabilitada; eventual limpeza em andamento será concluída."
    row.save()
    event(actor, "recovery.configured", app.pk, enabled=enabled, revision=row.revision)
    return row


@transaction.atomic
def claim():
    if not settings.SHINYD_RUNTIME_ENABLED:
        return None
    row = (
        RecoveryState.objects.filter(next_check_at__lte=timezone.now())
        .filter(Q(app__desired_state="running") | Q(job__isnull=False))
        .exclude(state__in=["open", "paused"])
        .order_by("next_check_at", "app_id")
        .first()
    )
    if row is None:
        return None
    row.lease_token = uuid.uuid4()
    row.next_check_at = timezone.now() + timedelta(seconds=90)
    row.save(update_fields=["lease_token", "next_check_at"])
    return row


def stop_recovery(row, state, message):
    if row.state != state or row.message != message:
        event(None, "recovery." + state, row.app_id, attempts=row.attempts)
    row.state, row.message = state, message
    row.next_attempt_at = row.healthy_since = None


@transaction.atomic
def observe_failure(job, code):
    if code == "process_oom" and activation_jobs.current(job):
        RecoveryState.objects.filter(job=job).update(halt_after_cleanup=True)


def allowed(row):
    app, source = row.app, row.source
    if not row.enabled:
        stop_recovery(
            row, "failed", "Processo encerrado. A recuperação automática está desabilitada."
        )
        return False
    if (
        not row.authorized_by_id
        or not manageable_apps(row.authorized_by).filter(pk=app.pk).exists()
    ):
        stop_recovery(
            row, "paused", "Recuperação suspensa: a conta autorizadora perdeu a permissão."
        )
        return False
    if not source or (
        app.revision != source.app_revision
        or app.config_revision != source.config_revision
        or app.priority.slug != source.runtime_spec["priority"]
        or app.data_enabled != (source.runtime_spec.get("data_mode") == "persistent")
    ):
        stop_recovery(
            row,
            "paused",
            "Há configuração pendente. Confira as alterações e reinicie ou ative manualmente.",
        )
        return False
    if (
        row.attempts >= 3
        or Job.objects.filter(
            app=app, operation="recover", created_at__gt=timezone.now() - WINDOW
        ).count()
        >= 3
    ):
        stop_recovery(
            row,
            "open",
            "Recuperação suspensa após três tentativas. Corrija a causa e habilite novamente "
            "ou reinicie manualmente. O limite de três tentativas em dez minutos permanece.",
        )
        return False
    return True


def schedule(row):
    if not allowed(row):
        return
    if row.next_attempt_at is None:
        row.next_attempt_at = timezone.now() + timedelta(seconds=DELAYS[row.attempts])
    row.state = "waiting"
    row.message = "Processo encerrado. Aguardando a próxima tentativa de recuperação."
    if timezone.now() < row.next_attempt_at:
        return
    try:
        # Savepoint: failure after candidate creation cannot leak a partial publication.
        with transaction.atomic():
            job = activation_jobs.enqueue(
                row.authorized_by, row.source.version_id, uuid.uuid4(), operation="recover"
            )
    except PermissionDenied:
        stop_recovery(row, "paused", "A conta autorizadora perdeu a permissão de publicação.")
    except Conflict:
        row.message = "Recuperação aguardando outra operação do aplicativo."
    except ValidationError:
        # A full global queue is temporary. Other preconditions need operator attention.
        if Job.objects.filter(status__in=PENDING).count() >= 100:
            row.message = "Recuperação aguardando espaço na fila de tarefas."
        else:
            stop_recovery(
                row,
                "paused",
                "Não foi possível preparar a recuperação. Confira a configuração e a chave.",
            )
    else:
        row.job = job
        row.attempts += 1
        row.next_attempt_at = None
        row.state = "recovering"
        row.message = (
            "Recuperação em andamento. A instância será verificada antes de abrir o acesso."
        )
        event(None, "recovery.queued", row.app_id, job=str(job.pk), attempt=row.attempts)


@transaction.atomic
def accept(claimed, observed_publication, result, unavailable=False):
    row = (
        RecoveryState.objects.select_related("app__priority", "source", "job", "authorized_by")
        .filter(pk=claimed.pk, lease_token=claimed.lease_token)
        .first()
    )
    if row is None:
        return
    now = timezone.now()
    previous_check = row.checked_at
    row.checked_at, row.next_check_at, row.lease_token = now, now + timedelta(seconds=10), None
    if row.job_id:
        if row.job.status in PENDING:
            row.state = "recovering"
        else:
            status = row.job.status
            row.job = None
            if row.halt_after_cleanup:
                stop_recovery(
                    row, "open", "Recuperação suspensa por falta de memória. Confira os recursos."
                )
            elif status == "succeeded":
                row.state, row.message = (
                    "unobserved",
                    "Recuperação concluída. Aguardando observação.",
                )
                row.source = row.app.active_publication
                row.healthy_since = None
                event(None, "recovery.succeeded", row.app_id, attempts=row.attempts)
            elif status == "cancelled":
                stop_recovery(
                    row, "paused", "Recuperação cancelada. Nenhuma nova tentativa será aberta."
                )
            else:
                row.next_attempt_at = None
                schedule(row)
    elif busy(row.app) or row.app.active_publication_id != observed_publication:
        pass  # A manual job/pointer change outranks a stale observation.
    elif row.app.desired_state != "running":
        stop_recovery(
            row, "stopped", "App parado por solicitação. Não será iniciado automaticamente."
        )
    elif row.app.active_publication_id is None:
        if row.source_id:
            schedule(row)  # A failed recovery after persistent-data cutover.
    elif unavailable or result is None:
        row.state, row.message = (
            "unknown",
            "Executor sem confirmação. Nenhuma nova instância será iniciada.",
        )
        row.healthy_since = None
    elif result["state"] == "ready":
        row.source = row.app.active_publication
        row.state, row.message = "healthy", "Processo em execução, confirmado pelo executor."
        row.next_attempt_at = None
        if (
            not row.healthy_since
            or not previous_check
            or now - previous_check > timedelta(seconds=30)
        ):
            row.healthy_since = now
        if now - row.healthy_since >= WINDOW:
            row.attempts = 0
    elif result["state"] == "failed" and result.get("error") in {
        "process_not_running",
        "process_oom",
    }:
        row.source = row.app.active_publication
        row.healthy_since = None
        AppInstance.objects.filter(publication_id=observed_publication).update(
            enabled=False, draining_for=None
        )
        if result["error"] == "process_oom":
            stop_recovery(
                row,
                "open",
                "Processo encerrado por falta de memória. "
                "Confira os recursos antes de reiniciar manualmente.",
            )
        else:
            schedule(row)
    else:
        row.state, row.message = (
            "unknown",
            "Estado do executor não permite recuperação automática. Consulte o administrador.",
        )
        row.healthy_since = None
    row.save()


def process_one():
    row = claim()
    if row is None:
        return False
    app = Application.objects.get(pk=row.app_id)
    publication = app.active_publication
    result, unavailable = None, False
    if not row.job_id and publication and not busy(app):
        try:
            result = runtime_client.inspect(publication)
        except (runtime_client.RuntimeUnavailable, runtime_client.RuntimeRejected):
            unavailable = True
    accept(row, app.active_publication_id, result, unavailable)
    return True

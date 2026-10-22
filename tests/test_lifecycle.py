import uuid
from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from control import activation_jobs as activation
from control import connections, lifecycle, runtime_client
from control.models import AppInstance, Application, AuditEvent, GatewayRequest, Job, OperationBatch
from control.services import Conflict, enable_data, set_environment
from tests import test_persistent_data as persistent
from tests.test_activation import expire, prepared

pytestmark = pytest.mark.django_db
runtime = persistent.runtime
enabled = persistent.enabled


def publish(app, actor):
    job = activation.enqueue(actor, prepared(app, actor).pk, uuid.uuid4())
    persistent.finish(job)
    app.refresh_from_db()
    return app.active_publication


def operation(app, actor, action, mode="immediate"):
    app.refresh_from_db()
    batch = lifecycle.enqueue(actor, action, mode, uuid.uuid4(), [lifecycle.state(app)])
    return batch.items.get().job


def tracked(pub):
    instance = AppInstance.objects.get(pk=pub.pk)
    request = GatewayRequest.objects.create(
        id=uuid.uuid4(), instance=instance, kind="websocket", upgraded=True
    )
    return instance, request


def test_stop_start_restart_keep_version_config_and_other_app(app, accounts, runtime, local_state):
    actor = accounts["publisher"]
    enable_data(actor, app.pk, app.revision)
    old = publish(app, actor)
    other = Application.objects.create(
        owner=actor, priority=app.priority, name="Other", slug="other"
    )
    other_pub = publish(other, actor)
    job = operation(app, actor, "stop")
    assert lifecycle.process_one()
    job.refresh_from_db()
    app.refresh_from_db()
    old.refresh_from_db()
    assert job.status == "succeeded" and old.status == "retired"
    assert not app.active_publication_id and app.last_version_id == old.version_id
    assert not AppInstance.objects.get(pk=old.pk).enabled
    assert ("remove", other_pub.pk) not in runtime[1]
    assert app.data_enabled and app.desired_state == "stopped"
    set_environment(actor, app.pk, app.config_revision, "TOKEN", "new-secret", True)
    start = operation(app, actor, "start")
    persistent.finish(start)
    app.refresh_from_db()
    assert start.status == "succeeded"
    assert start.version_id == old.version_id and start.publication_id != old.pk
    assert app.active_publication.config_revision == 1
    assert app.active_publication.runtime_spec["data_mode"] == "persistent"
    restarted = operation(app, actor, "restart")
    persistent.finish(restarted)
    assert restarted.status == "succeeded" and restarted.publication_id != start.publication_id
    assert restarted.version_id == start.version_id
    assert "new-secret" not in str(list(AuditEvent.objects.values()))
    assert other.active_publication_id == other_pub.pk


def test_stop_drain_rejects_new_ws_and_completes_after_close(app, accounts, runtime):
    app.visibility = "public"
    app.save()
    old = publish(app, accounts["publisher"])
    instance, request = tracked(old)
    job = operation(app, accounts["publisher"], "stop", "drain")
    assert lifecycle.process_one()
    job.refresh_from_db()
    assert job.phase == "waiting_connections" and not job.operation_item.committed
    assert (
        connections.track(
            operation="open",
            request_id=str(uuid.uuid4()),
            host=instance.host,
            credential="",
            kind="websocket",
        )["reason"]
        == "draining"
    )
    assert connections.track(
        operation="pulse",
        request_id=str(request.pk),
        host=instance.host,
        credential="",
        kind="websocket",
    )["allowed"]
    assert ("remove", old.pk) not in runtime[1]
    request.delete()
    expire(job)
    lifecycle.process_one()
    job.refresh_from_db()
    assert job.status == "succeeded"
    assert not AppInstance.objects.get(pk=old.pk).enabled


@pytest.mark.parametrize(
    "reason", ["cancel", "timeout", "uncertain", "permission", "config", "disabled"]
)
def test_stop_drain_abort_preserves_app_and_reopens_admission(
    app, accounts, runtime, reason, settings
):
    old = publish(app, accounts["publisher"])
    instance, request = tracked(old)
    job = operation(app, accounts["publisher"], "stop", "drain")
    lifecycle.process_one()
    if reason == "cancel":
        Job.objects.filter(pk=job.pk).update(cancel_requested=True)
    elif reason == "timeout":
        job.operation_item.drain_started_at = timezone.now() - timedelta(minutes=6)
        job.operation_item.save()
    elif reason == "uncertain":
        GatewayRequest.objects.filter(pk=request.pk).update(
            heartbeat_at=timezone.now() - timedelta(minutes=2)
        )
    elif reason == "permission":
        accounts["publisher"].is_active = False
        accounts["publisher"].save()
    elif reason == "config":
        Application.objects.filter(pk=app.pk).update(config_revision=1)
    else:
        settings.SHINYD_RUNTIME_ENABLED = False
    expire(job)
    lifecycle.process_one()
    job.refresh_from_db()
    instance.refresh_from_db()
    app.refresh_from_db()
    assert job.status in {"failed", "cancelled"}
    assert instance.enabled and not instance.draining_for_id
    assert app.active_publication_id == old.pk
    assert ("remove", old.pk) not in runtime[1]


def test_committed_stop_survives_lost_reply_revocation_and_cancel(
    app, accounts, runtime, monkeypatch, client
):
    actor = accounts["publisher"]
    old = publish(app, actor)
    _, request = tracked(old)
    job = operation(app, actor, "stop")
    retire = runtime_client.retire

    def lost_reply(pub):
        retire(pub)
        raise runtime_client.RuntimeUnavailable

    monkeypatch.setattr(runtime_client, "retire", lost_reply)
    lifecycle.process_one()
    job.refresh_from_db()
    assert job.status == "running" and job.operation_item.committed
    assert GatewayRequest.objects.filter(pk=request.pk).exists()
    with pytest.raises(Conflict):
        activation.enqueue(actor, prepared(app, actor).pk, uuid.uuid4())
    with pytest.raises(Conflict):
        enable_data(actor, app.pk, app.revision)
    client.force_login(actor)
    client.post(reverse("job_cancel", args=[job.pk]))
    job.refresh_from_db()
    assert not job.cancel_requested
    actor.is_active = False
    actor.save()
    Job.objects.filter(pk=job.pk).update(cancel_requested=True)
    monkeypatch.setattr(runtime_client, "retire", retire)
    expire(job)
    lifecycle.process_one()
    job.refresh_from_db()
    assert job.status == "succeeded" and runtime[1].count(("remove", old.pk)) == 2
    assert not GatewayRequest.objects.filter(pk=request.pk).exists()


def test_stop_old_worker_cannot_commit_or_finish(app, accounts, runtime):
    publish(app, accounts["publisher"])
    queued = operation(app, accounts["publisher"], "stop")
    old = activation.claim("stop_app")
    expire(queued)
    new = activation.claim("stop_app")
    assert not lifecycle.checkpoint(old)
    assert lifecycle.checkpoint(new)
    lifecycle.complete(old)
    queued.refresh_from_db()
    assert queued.status == "running"
    lifecycle.complete(new)
    queued.refresh_from_db()
    assert queued.status == "succeeded"


def test_batch_partial_results_stale_state_idempotency_and_permission(app, accounts, runtime):
    actor = accounts["publisher"]
    publish(app, actor)
    other = Application.objects.create(
        owner=actor, priority=app.priority, name="Other", slug="other"
    )
    outsider = Application.objects.create(
        owner=accounts["outsider"], priority=app.priority, name="Private", slug="private"
    )
    target = lifecycle.state(app)
    target["config_revision"] += 1
    targets = [target, lifecycle.state(other), lifecycle.state(outsider)]
    key = uuid.uuid4()
    batch = lifecycle.enqueue(actor, "stop", "immediate", key, targets)
    assert batch.items.get(app=app).status == "failed"
    assert batch.items.get(app=other).status == "succeeded"
    assert batch.items.get(app=outsider).status == "failed"
    assert not batch.items.exclude(job=None).exists()
    assert lifecycle.enqueue(actor, "stop", "immediate", key, targets).pk == batch.pk
    with pytest.raises(Conflict):
        lifecycle.enqueue(actor, "start", "immediate", key, targets)
    for targets in [[], [lifecycle.state(app)] * 101]:
        with pytest.raises(ValidationError):
            lifecycle.enqueue(actor, "stop", "immediate", uuid.uuid4(), targets)


def test_batch_independent_jobs_and_capacity(app, accounts, runtime):
    actor = accounts["publisher"]
    old = publish(app, actor)
    other = Application.objects.create(
        owner=actor, priority=app.priority, name="Other", slug="other"
    )
    publish(other, actor)
    for _ in range(99):
        Job.objects.create(app=app, actor=actor, kind="inspect_upload")
    targets = [lifecycle.state(app), lifecycle.state(other)]
    key = uuid.uuid4()
    batch = lifecycle.enqueue(actor, "stop", "immediate", key, targets)
    assert batch.items.get(app=app).job_id
    assert batch.items.get(app=other).status == "failed"
    assert "Fila cheia" in batch.items.get(app=other).message
    lifecycle.process_one()
    assert ("remove", old.pk) in runtime[1]
    assert lifecycle.enqueue(actor, "stop", "immediate", key, targets).pk == batch.pk
    assert batch.items.exclude(job=None).count() == 1


def test_noop_and_never_published_start(app, accounts, runtime):
    actor = accounts["publisher"]
    for action in ["start", "restart"]:
        batch = lifecycle.enqueue(actor, action, "drain", uuid.uuid4(), [lifecycle.state(app)])
        assert batch.items.get().status == "failed"
        assert not batch.items.get().job_id
    publish(app, actor)
    batch = lifecycle.enqueue(actor, "start", "immediate", uuid.uuid4(), [lifecycle.state(app)])
    assert batch.items.get().status == "succeeded" and not batch.items.get().job_id


def test_browser_confirmation_all_frozen_csrf_permissions_and_polling(
    app, accounts, runtime, client
):
    actor = accounts["publisher"]
    publish(app, actor)
    client.force_login(actor)
    url = reverse("operations")
    assert client.get(url).status_code == 200
    first = client.post(url, {"action": "stop", "switch_mode": "drain", "all_apps": "on"})
    assert first.status_code == 200 and not OperationBatch.objects.exists()
    token = first.context["confirmation"].initial["confirmation"]
    other = Application.objects.create(
        owner=actor, priority=app.priority, name="Later", slug="later"
    )
    assert client.post(url, {"confirmation": token}).status_code == 400
    csrf = Client(enforce_csrf_checks=True)
    csrf.force_login(actor)
    assert csrf.post(url, {"confirmation": token, "acknowledge": "on"}).status_code == 403
    response = client.post(url, {"confirmation": token, "acknowledge": "on"})
    assert response.status_code == 302
    batch = OperationBatch.objects.get()
    assert list(batch.items.values_list("app_id", flat=True)) == [app.pk]
    assert not batch.items.filter(app=other).exists()
    detail = client.get(response.url)
    assert detail.context["pending"] and b"every 3s" in detail.content
    client.force_login(accounts["viewer"])
    assert client.get(response.url).status_code == 404
    assert (
        client.post(url, {"action": "stop", "switch_mode": "drain", "apps": [app.pk]}).status_code
        == 200
    )
    assert client.post(url, {"confirmation": token, "acknowledge": "on"}).status_code == 409
    assert OperationBatch.objects.count() == 1
    app.owner = accounts["admin"]
    app.save()
    client.force_login(actor)
    detail = client.get(response.url)
    assert not detail.context["items"] and detail.context["hidden"] == 1
    assert app.name.encode() not in detail.content

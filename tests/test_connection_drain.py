import uuid
from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse
from django.utils import timezone

from control import activation_jobs as activation
from control import connections
from control.gateway_auth import exchange, issue_ticket
from control.models import AppInstance, Application, GatewayRequest, Job, Publication
from control.services import Conflict, enable_data
from tests import test_persistent_data as persistent
from tests.test_activation import expire, prepared
from tests.test_gateway_auth import call_api

pytestmark = pytest.mark.django_db
runtime = persistent.runtime
enabled = persistent.enabled


def publish(app, actor, mode="immediate"):
    return activation.enqueue(actor, prepared(app, actor).pk, uuid.uuid4(), switch_mode=mode)


def connection(instance, credential="", kind="websocket"):
    return dict(request_id=str(uuid.uuid4()), host=instance.host, credential=credential, kind=kind)


def tick(job):
    expire(job)
    activation.process_one()
    job.refresh_from_db()


@pytest.mark.parametrize("public", [False, True])
def test_drain_preserves_existing_and_blocks_new_ws_but_waits_for_transfers(
    app, accounts, runtime, client, public, settings
):
    settings.ALLOWED_HOSTS = [*settings.ALLOWED_HOSTS, "panel.localhost"]
    actor = accounts["publisher"]
    app.visibility = "public" if public else "private"
    app.save()
    first = publish(app, actor)
    persistent.finish(first)
    instance = AppInstance.objects.get(pk=first.publication_id)
    client.force_login(actor)
    credential = (
        ""
        if public
        else exchange(instance.host, issue_ticket(actor, client.session.session_key, instance))[
            "credential"
        ]
    )
    ws = connection(instance, credential)
    assert connections.track(operation="open", **ws)["allowed"]
    assert connections.counts(instance.pk)["opening"] == 1
    connections.track(operation="upgrade", **ws)
    second = publish(app, actor, "drain")
    tick(second)
    assert second.phase == "waiting_connections"
    app.refresh_from_db()
    assert app.active_publication_id == first.publication_id
    assert ("remove", first.publication_id) not in runtime[1]
    assert connections.track(operation="pulse", **ws)["allowed"]
    assert connections.track(operation="open", **connection(instance, credential)) == {
        "allowed": False,
        "reason": "draining",
    }
    transfer = connection(instance, credential, "http")
    assert connections.track(operation="open", **transfer)["allowed"]
    page = client.get(reverse("app_detail", args=[app.pk]))
    assert "1 WebSocket abertas" in page.content.decode()
    assert "Novas conexões interativas" in page.content.decode()
    handoff = client.get(
        reverse("gateway_connect", args=[instance.pk]), HTTP_HOST="panel.localhost"
    )
    assert handoff.status_code == 503
    assert "Atualização em espera" in handoff.content.decode()
    connections.track(operation="close", **ws)
    tick(second)
    assert second.phase == "waiting_connections"
    assert connections.counts(instance.pk)["http"] == 1
    connections.track(operation="close", **transfer)
    tick(second)
    assert second.status == "succeeded"
    assert ("remove", first.publication_id) in runtime[1]
    assert not connections.track(operation="open", **connection(instance, credential))["allowed"]


@pytest.mark.parametrize("failure", ["cancel", "deadline", "uncertain", "config", "permission"])
def test_failed_wait_reopens_previous_without_retiring_it(app, accounts, runtime, failure):
    actor = accounts["publisher"]
    app.visibility = "public"
    app.save()
    first = publish(app, actor)
    persistent.finish(first)
    instance = AppInstance.objects.get(pk=first.publication_id)
    ws = connection(instance)
    connections.track(operation="open", **ws)
    second = publish(app, actor, "drain")
    tick(second)
    if failure == "cancel":
        Job.objects.filter(pk=second.pk).update(cancel_requested=True)
    elif failure == "deadline":
        Publication.objects.filter(pk=second.publication_id).update(
            drain_started_at=timezone.now() - timedelta(minutes=5, seconds=1)
        )
    elif failure == "uncertain":
        GatewayRequest.objects.filter(pk=ws["request_id"]).update(
            heartbeat_at=timezone.now() - timedelta(seconds=61)
        )
    elif failure == "config":
        Application.objects.filter(pk=app.pk).update(config_revision=1)
    else:
        actor.is_active = False
        actor.save()
    persistent.finish(second)
    instance.refresh_from_db()
    app.refresh_from_db()
    assert second.status in {"failed", "cancelled"}
    assert instance.enabled and instance.draining_for_id is None
    assert app.active_publication_id == first.publication_id
    assert ("remove", first.publication_id) not in runtime[1]
    assert connections.track(operation="open", **connection(instance))["allowed"]
    if failure == "uncertain":
        assert GatewayRequest.objects.filter(pk=ws["request_id"]).exists()
        assert "Não foi possível confirmar" in second.message


def test_persistent_drain_waits_before_removing_only_writer(app, accounts, runtime):
    actor = accounts["publisher"]
    app.visibility = "public"
    app.save()
    enable_data(actor, app.pk, app.revision)
    first = publish(app, actor)
    persistent.finish(first)
    instance = AppInstance.objects.get(pk=first.publication_id)
    ws = connection(instance)
    connections.track(operation="open", **ws)
    second = publish(app, actor, "drain")
    tick(second)  # Probe readiness.
    tick(second)  # Probe removed; existing connection is still reserved.
    assert second.phase == "waiting_connections"
    assert not second.publication.data_cutover
    assert ("remove", first.publication_id) not in runtime[1]
    assert ("start", second.publication_id) not in runtime[1]
    connections.track(operation="close", **ws)
    tick(second)  # Cutover is now safe, but persistent candidate is not started yet.
    assert second.publication.data_cutover
    Publication.objects.filter(pk=second.publication_id).update(
        drain_started_at=timezone.now() - timedelta(minutes=6)
    )  # Completed drain deadline must not abort the subsequent startup.
    persistent.finish(second)
    assert second.status == "succeeded"
    assert runtime[1].index(("remove", first.publication_id)) < runtime[1].index(
        ("start", second.publication_id)
    )


def test_requests_cannot_be_replayed_rebound_or_revived_after_close(app, accounts, runtime):
    app.visibility = "public"
    app.save()
    first = publish(app, accounts["publisher"])
    persistent.finish(first)
    instance = AppInstance.objects.get(pk=first.publication_id)
    ws = connection(instance)
    assert call_api("/v2/request", {"operation": "open", **ws})[0] == 200
    assert call_api("/v2/request", {"operation": "open", **ws})[0] == 400
    with pytest.raises(PermissionDenied):
        connections.track(operation="close", **{**ws, "host": "other.apps.localhost"})
    with pytest.raises(PermissionDenied):
        connections.track(operation="pulse", **{**ws, "kind": "http"})
    app.visibility = "private"
    app.save()
    assert not connections.track(operation="pulse", **ws)["allowed"]
    assert connections.track(operation="close", **ws)["allowed"]
    app.visibility = "public"
    app.save()
    assert not connections.track(operation="pulse", **ws)["allowed"]
    assert not connections.track(operation="upgrade", **ws)["allowed"]
    assert connections.counts(instance.pk)["total"] == 0


def test_mode_is_validated_and_bound_to_idempotency(app, accounts, runtime, client):
    version = prepared(app, accounts["publisher"])
    key = uuid.uuid4()
    with pytest.raises(ValidationError):
        activation.enqueue(accounts["publisher"], version.pk, key, switch_mode="invalid")
    client.force_login(accounts["viewer"])
    url = reverse("version_activate", args=[version.pk])
    assert client.post(url, {"switch_mode": "drain", "acknowledge": True}).status_code == 404
    client.force_login(accounts["publisher"])
    assert (
        client.post(
            url, {"switch_mode": "drain", "acknowledge": True, "idempotency_key": key}
        ).status_code
        == 302
    )
    job = Job.objects.get(idempotency_key=key)
    assert job.publication.switch_mode == "drain"
    with pytest.raises(Conflict):
        activation.enqueue(accounts["publisher"], version.pk, key, switch_mode="immediate")
    persistent.finish(job)
    assert job.status == "succeeded"  # No previous publication, no wait needed.

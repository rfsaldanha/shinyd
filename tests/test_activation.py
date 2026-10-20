import copy
import uuid
from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse
from django.utils import timezone

from control import activation_jobs as activation
from control import runtime_client
from control.gateway_auth import authorize
from control.models import AppInstance, Application, AuditEvent, Job, Publication, Version
from control.services import Conflict, set_environment

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def enabled(settings):
    settings.DEBUG = True
    settings.SHINYD_RUNTIME_ENABLED = True
    settings.SHINYD_PANEL_ORIGIN = "http://panel.localhost"
    settings.SHINYD_APP_DOMAIN = "apps.localhost"


def prepared(app, actor):
    return Version.objects.create(
        app=app,
        created_by=actor,
        status="prepared",
        source_hash="a" * 64,
        source_path="sources/fixture.zip",
        image_digest="sha256:" + "b" * 64,
        build_manifest={
            "app_id": str(app.pk),
            "source_hash": "a" * 64,
            "cache_key": "c" * 64,
            "image": "sha256:" + "b" * 64,
        },
    )


def queue(app, accounts):
    version = prepared(app, accounts["publisher"])
    return activation.enqueue(accounts["publisher"], version.pk, uuid.uuid4())


def ready(pub):
    return {
        "id": str(pub.pk),
        **pub.runtime_spec,
        "state": "ready",
        "desired": "running",
        "image": pub.version.image_digest,
        "target": f"unix:/run/shinyd-runtime/apps/{pub.pk}.sock",
    }


@pytest.fixture
def runtime(monkeypatch):
    records, starts, retired = {}, [], []

    def start(pub):
        starts.append(pub.pk)
        records[pub.pk] = ready(pub)
        return records[pub.pk]

    def retire(pub):
        retired.append(pub.pk)
        records[pub.pk] = {**ready(pub), "state": "removed", "desired": "removed", "target": ""}
        return records[pub.pk]

    monkeypatch.setattr(runtime_client, "inspect", lambda p: records.get(p.pk))
    monkeypatch.setattr(runtime_client, "ensure", start)
    monkeypatch.setattr(runtime_client, "retire", retire)
    return records, starts, retired


def expire(job):
    Job.objects.filter(pk=job.pk).update(lease_until=timezone.now() - timedelta(seconds=1))


def test_activation_enables_only_checked_route_and_respects_access(app, accounts, runtime, client):
    job = queue(app, accounts)
    instance = AppInstance.objects.get(pk=job.publication_id)
    assert not instance.enabled and not instance.target
    assert authorize(instance.host, "")["allowed"] is False
    activation.process_one()
    job.refresh_from_db()
    app.refresh_from_db()
    instance.refresh_from_db()
    assert job.status == "succeeded" and app.active_publication_id == job.publication_id
    assert instance.enabled and instance.target.endswith(f"/{instance.pk}.sock")
    assert authorize(instance.host, "")["allowed"] is False  # private app
    app.visibility = "public"
    app.save()
    assert authorize(instance.host, "")["allowed"] is True
    client.force_login(accounts["publisher"])
    assert b"Abrir aplicativo" in client.get(reverse("app_detail", args=[app.pk])).content
    assert AuditEvent.objects.filter(action="publication.activated").count() == 1


@pytest.mark.parametrize("domain", ["", "apps.localhost:99999", "example.org", "a" * 260])
def test_invalid_operator_domain_leaves_no_candidate(app, accounts, settings, domain):
    settings.SHINYD_APP_DOMAIN = domain
    with pytest.raises(ValidationError):
        queue(app, accounts)
    assert not Job.objects.exists() and not Publication.objects.exists()
    assert not AppInstance.objects.exists()


def test_failed_candidate_preserves_previous(app, accounts, runtime, monkeypatch):
    first = queue(app, accounts)
    activation.process_one()
    previous = AppInstance.objects.get(pk=first.publication_id)
    candidate = queue(app, accounts)
    monkeypatch.setattr(
        runtime_client,
        "ensure",
        lambda p: {
            **ready(p),
            "state": "failed",
            "target": "",
            "error": "application_readiness_failed",
        },
    )
    activation.process_one()
    candidate.refresh_from_db()
    app.refresh_from_db()
    previous.refresh_from_db()
    assert candidate.status == "failed"
    assert app.active_publication_id == previous.pk and previous.enabled
    assert runtime[2] == [candidate.publication_id]
    assert first.publication_id in runtime[0]


def test_successful_switch_does_not_retarget_old_host(app, accounts, runtime):
    first = queue(app, accounts)
    activation.process_one()
    previous = AppInstance.objects.get(pk=first.publication_id)
    old_target = previous.target
    second = queue(app, accounts)
    activation.process_one()
    previous.refresh_from_db()
    app.refresh_from_db()
    assert app.active_publication_id == second.publication_id
    assert not previous.enabled and previous.target == old_target
    assert runtime[2] == [first.publication_id]
    assert not authorize(previous.host, "")["allowed"]


def test_lost_response_reconciles_same_candidate_without_second_start(
    app, accounts, runtime, monkeypatch
):
    job = queue(app, accounts)
    original = runtime_client.ensure

    def lost(pub):
        original(pub)
        raise runtime_client.RuntimeUnavailable

    monkeypatch.setattr(runtime_client, "ensure", lost)
    activation.process_one()
    expire(job)
    activation.process_one()
    job.refresh_from_db()
    assert job.status == "succeeded" and runtime[1] == [job.publication_id]


@pytest.mark.parametrize(
    "change", ["cancel", "permission", "config", "disabled", "timeout", "image"]
)
def test_change_during_launch_prevents_switch(
    app, accounts, runtime, monkeypatch, settings, change
):
    job = queue(app, accounts)

    def changed(pub):
        if change == "cancel":
            Job.objects.filter(pk=job.pk).update(cancel_requested=True)
        elif change == "permission":
            accounts["publisher"].is_active = False
            accounts["publisher"].save()
        elif change == "config":
            Application.objects.filter(pk=app.pk).update(config_revision=1)
        elif change == "disabled":
            settings.SHINYD_RUNTIME_ENABLED = False
        elif change == "timeout":
            Job.objects.filter(pk=job.pk).update(created_at=timezone.now() - timedelta(minutes=16))
        result = ready(pub)
        if change == "image":
            result["image"] = "sha256:" + "f" * 64
        return result

    monkeypatch.setattr(runtime_client, "ensure", changed)
    activation.process_one()
    app.refresh_from_db()
    job.refresh_from_db()
    assert app.active_publication_id is None
    assert job.status in {"cancelled", "failed"}
    assert runtime[2] == [job.publication_id]


def test_cancel_before_submit_and_cleanup_retry(app, accounts, runtime, monkeypatch):
    job = queue(app, accounts)
    Job.objects.filter(pk=job.pk).update(cancel_requested=True)
    original = runtime_client.retire

    def unavailable(pub):
        raise runtime_client.RuntimeUnavailable

    monkeypatch.setattr(runtime_client, "retire", unavailable)
    activation.process_one()
    job.refresh_from_db()
    assert job.status == "running" and job.publication.status == "cancelled"
    assert job.phase == "cleaning_candidate" and "Ativação cancelada" in job.message
    assert runtime[1] == []
    monkeypatch.setattr(runtime_client, "retire", original)
    expire(job)
    activation.process_one()
    job.refresh_from_db()
    assert job.status == "cancelled" and runtime[2] == [job.publication_id]


def test_stale_worker_cannot_publish_after_new_lease(app, accounts, runtime):
    queued = queue(app, accounts)
    old = activation.claim()
    expire(old)
    new = activation.claim()
    pub = queued.publication
    assert not activation.decide(old, ready(pub))
    assert activation.decide(new, ready(pub))


def test_authorization_idempotency_and_configuration_limits(app, accounts, local_state):
    version = prepared(app, accounts["publisher"])
    key = uuid.uuid4()
    with pytest.raises(PermissionDenied):
        activation.enqueue(accounts["viewer"], version.pk, key)
    set_environment(accounts["publisher"], app.pk, 0, "TOKEN", "PRIVATE_SENTINEL", True)
    with pytest.raises(ValidationError):
        activation.enqueue(accounts["publisher"], version.pk, key)
    app.environment.all().delete()  # fixture only; the product has no deletion flow yet
    job = activation.enqueue(accounts["publisher"], version.pk, key)
    assert activation.enqueue(accounts["publisher"], version.pk, key).pk == job.pk
    with pytest.raises(Conflict):
        activation.enqueue(accounts["publisher"], version.pk, uuid.uuid4())
    with pytest.raises(Conflict):
        activation.enqueue(accounts["admin"], version.pk, key)
    assert "PRIVATE_SENTINEL" not in str(list(AuditEvent.objects.values("details")))


def test_admission_wait_is_bounded_and_cancellable(app, accounts, runtime, monkeypatch):
    job = queue(app, accounts)

    def denied(pub):
        raise runtime_client.RuntimeRejected("admission_denied")

    monkeypatch.setattr(runtime_client, "ensure", denied)
    activation.process_one()
    job.refresh_from_db()
    assert job.status == "running"
    Job.objects.filter(pk=job.pk).update(created_at=timezone.now() - timedelta(minutes=16))
    expire(job)
    activation.process_one()
    job.refresh_from_db()
    assert job.status == "failed" and runtime[2] == [job.publication_id]


def test_post_requires_acknowledgement_and_cancel_after_switch_is_refused(
    app, accounts, runtime, monkeypatch, client
):
    client.force_login(accounts["publisher"])
    version = prepared(app, accounts["publisher"])
    url = reverse("version_activate", args=[version.pk])
    assert client.post(url, {"idempotency_key": uuid.uuid4()}).status_code == 200
    assert not Job.objects.exists()
    assert (
        client.post(url, {"idempotency_key": uuid.uuid4(), "acknowledge": True}).status_code == 302
    )
    activation.process_one()
    second = queue(app, accounts)
    monkeypatch.setattr(
        runtime_client,
        "retire",
        lambda p: (_ for _ in ()).throw(runtime_client.RuntimeUnavailable()),
    )
    activation.process_one()
    second.refresh_from_db()
    assert second.status == "running" and second.publication.status == "active"
    client.post(reverse("job_cancel", args=[second.pk]))
    second.refresh_from_db()
    assert not second.cancel_requested


@pytest.mark.parametrize("mutation", ["target", "app_id", "artifact", "extra", "duplicate"])
def test_runtime_client_rejects_mismatched_or_unsafe_responses(
    app, accounts, monkeypatch, mutation
):
    import io
    import json

    pub = queue(app, accounts).publication
    value = copy.deepcopy(ready(pub))
    value["target"] = f"unix:/run/shinyd-runtime/apps/{pub.pk}.sock"
    if mutation == "target":
        value["target"] = "http://127.0.0.1:22"
    elif mutation == "app_id":
        value["app_id"] = str(uuid.uuid4())
    elif mutation == "artifact":
        value["artifact"] = "f" * 64
    elif mutation == "extra":
        value["private"] = "PRIVATE_SENTINEL"
    raw = json.dumps(value)
    if mutation == "duplicate":
        raw = raw.replace('"state": "ready"', '"state": "ready", "state": "ready"')

    class Connection:
        def __init__(self, path, uid):
            assert uid == 0

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            response = io.BytesIO(raw.encode())
            response.status = 200
            return response

        def close(self):
            pass

    monkeypatch.setattr(runtime_client, "UnixConnection", Connection)
    with pytest.raises(runtime_client.RuntimeUnavailable):
        runtime_client.inspect(pub)

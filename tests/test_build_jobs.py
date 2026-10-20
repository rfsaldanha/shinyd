import uuid
from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.utils import timezone

from control import build_jobs, builder_client
from control.jobs import process_one
from control.models import AuditEvent, Job
from control.services import Conflict
from tests.test_jobs import send

pytestmark = pytest.mark.django_db


def queued(app, accounts):
    upload = send(app, accounts["publisher"])
    process_one()
    return build_jobs.enqueue(accounts["publisher"], upload.version_id, "approved", uuid.uuid4())


def result(job, status="running"):
    return {
        "id": str(job.pk),
        "spec": builder_client.spec_for(job),
        "status": status,
        "phase": "prepared" if status == "succeeded" else "restoring",
        "error": "",
        "artifact": {
            "schema_version": 1,
            "app_id": str(job.app_id),
            "cache_key": "b" * 64,
            "source_hash": job.version.source_hash,
            "entrypoint": job.version.entrypoint,
            "image": "sha256:" + "a" * 64,
            "r_version": "4.4.3",
        }
        if status == "succeeded"
        else None,
    }


def expire(job):
    Job.objects.filter(pk=job.pk).update(lease_until=timezone.now() - timedelta(seconds=1))


def test_success_records_immutable_artifact_without_changing_publication(
    app,
    accounts,
    local_state,
    monkeypatch,
):
    job = queued(app, accounts)
    monkeypatch.setattr(builder_client, "inspect", lambda j: result(j, "succeeded"))
    monkeypatch.setattr(builder_client, "submit", lambda _: pytest.fail("duplicate submission"))
    assert process_one()
    job.refresh_from_db()
    assert job.status == "succeeded" and job.version.status == "prepared"
    assert job.version.image_digest == "sha256:" + "a" * 64
    assert job.version.build_manifest["source_hash"] == job.version.source_hash
    app.refresh_from_db()
    assert app.active_publication is None
    assert AuditEvent.objects.filter(action="build.succeeded", target=str(job.pk)).exists()


def test_lost_submit_response_reconciles_same_id(app, accounts, local_state, monkeypatch):
    job = queued(app, accounts)
    accepted = []
    monkeypatch.setattr(builder_client, "inspect", lambda _: None)

    def lost(j):
        accepted.append(j.pk)
        raise builder_client.BuilderUnavailable

    monkeypatch.setattr(builder_client, "submit", lost)
    process_one()
    job.refresh_from_db()
    assert job.status == "running"
    expire(job)
    monkeypatch.setattr(builder_client, "inspect", lambda j: result(j, "succeeded"))
    process_one()
    job.refresh_from_db()
    assert job.status == "succeeded" and accepted == [job.pk] and job.attempts == 1


def test_stale_worker_cannot_finish_new_lease(app, accounts, local_state):
    job = queued(app, accounts)
    old = build_jobs.claim()
    expire(job)
    new = build_jobs.claim()
    assert not build_jobs.record(old, result(old, "succeeded"))
    assert build_jobs.record(new, result(new))


def test_permission_revocation_cancels_without_submit(app, accounts, local_state, monkeypatch):
    job = queued(app, accounts)
    accounts["publisher"].is_active = False
    accounts["publisher"].save()
    cancelled = []

    def cancel(j):
        cancelled.append(j.pk)
        return {**result(j), "status": "cancelled", "phase": "cancelled"}

    monkeypatch.setattr(builder_client, "cancel", cancel)
    monkeypatch.setattr(builder_client, "submit", lambda _: pytest.fail("must not submit"))
    process_one()
    job.refresh_from_db()
    assert cancelled == [job.pk] and job.status == "cancelled"
    assert job.version.image_digest == ""


def test_cancel_racing_success_does_not_expose_artifact(app, accounts, local_state):
    job = queued(app, accounts)
    claimed = build_jobs.claim()
    Job.objects.filter(pk=job.pk).update(cancel_requested=True)
    build_jobs.record(claimed, result(claimed, "succeeded"))
    job.refresh_from_db()
    assert job.status == "cancelled" and job.version.image_digest == ""


def test_object_permissions_idempotency_and_form(
    client,
    app,
    accounts,
    local_state,
    monkeypatch,
):
    job = queued(app, accounts)
    assert (
        build_jobs.enqueue(
            accounts["publisher"],
            job.version_id,
            "approved",
            job.idempotency_key,
        ).pk
        == job.pk
    )
    with pytest.raises(Conflict):
        build_jobs.enqueue(accounts["publisher"], job.version_id, "other", job.idempotency_key)
    with pytest.raises(PermissionDenied):
        build_jobs.enqueue(accounts["outsider"], job.version_id, "approved", uuid.uuid4())
    monkeypatch.setattr(
        builder_client,
        "environments",
        lambda: [
            {"id": "approved", "r_version": "4.4.3"},
        ],
    )
    client.force_login(accounts["outsider"])
    assert client.get(reverse("version_prepare", args=[job.version_id])).status_code == 404
    client.force_login(accounts["publisher"])
    assert b"R 4.4.3" in client.get(reverse("version_prepare", args=[job.version_id])).content
    assert client.post(reverse("job_cancel", args=[job.pk])).status_code == 302
    job.refresh_from_db()
    assert job.cancel_requested


def test_unavailable_catalog_shows_safe_message(client, app, accounts, local_state, monkeypatch):
    job = queued(app, accounts)

    def unavailable():
        raise builder_client.BuilderUnavailable

    monkeypatch.setattr(builder_client, "environments", unavailable)
    client.force_login(accounts["publisher"])
    response = client.get(reverse("version_prepare", args=[job.version_id]))
    assert response.status_code == 503 and b"indispon" in response.content

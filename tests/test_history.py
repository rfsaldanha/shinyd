import uuid

import pytest
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client
from django.urls import reverse

from control import activation_jobs as activation
from control import runtime_client
from control.environment import reveal
from control.history_views import CONFIRMATION_SALT
from control.models import AppGrant, AppInstance, AuditEvent, Job, Publication, Version
from control.services import Conflict, enable_data, remove_environment, set_environment
from tests import test_persistent_data as persistence
from tests.test_activation import prepared

pytestmark = pytest.mark.django_db
enabled, runtime, finish = persistence.enabled, persistence.runtime, persistence.finish


def history(app, actor):
    jobs = []
    for _ in range(2):
        job = activation.enqueue(actor, prepared(app, actor).pk, uuid.uuid4())
        finish(job)
        jobs.append(job)
    app.refresh_from_db()
    return Publication.objects.get(pk=jobs[0].publication_id), jobs[1]


def rollback(app, actor, source, key=None, state=None):
    app.refresh_from_db()
    return activation.enqueue(
        actor,
        source.version_id,
        key or uuid.uuid4(),
        restored_from=source.pk,
        expected_state=state or activation.rollback_state(app, source),
    )


def test_rollback_reuses_code_with_current_secrets_and_unique_route(
    app, accounts, runtime, local_state
):
    actor = accounts["publisher"]
    enable_data(actor, app.pk, app.revision)
    set_environment(actor, app.pk, 0, "TOKEN", "old-synthetic-secret", True)
    set_environment(actor, app.pk, 1, "REMOVED", "removed-synthetic-secret", True)
    source, previous = history(app, actor)
    old_snapshot = bytes(source.environment_snapshot)
    set_environment(actor, app.pk, 2, "TOKEN", "current-synthetic-secret", True)
    remove_environment(actor, app.pk, 3, "REMOVED")
    job = rollback(app, actor, source)
    assert job.publication.previous_id == previous.publication_id
    assert job.publication.restored_from_id == source.pk
    assert job.publication.config_revision == 4
    assert reveal(job.publication) == {"TOKEN": "current-synthetic-secret"}
    assert reveal(source) == {
        "TOKEN": "old-synthetic-secret",
        "REMOVED": "removed-synthetic-secret",
    }
    assert job.publication_id not in {source.pk, previous.publication_id}
    assert job.publication.version.image_digest == source.version.image_digest
    assert job.publication.runtime_spec["data_mode"] == "persistent"
    finish(job)
    assert job.status == "succeeded"
    app.refresh_from_db()
    source.refresh_from_db()
    assert app.active_publication_id == job.publication_id
    assert app.config_revision == 4 and app.data_enabled
    assert source.status == "retired" and bytes(source.environment_snapshot) == old_snapshot
    assert Version.objects.count() == 2
    assert set(AppInstance.objects.filter(enabled=True).values_list("pk", flat=True)) == {
        job.publication_id
    }
    assert runtime[1].index(("remove", previous.publication_id)) < runtime[1].index(
        ("start", job.publication_id)
    )
    details = AuditEvent.objects.get(action="rollback.queued").details
    assert details["restored_from"] == str(source.pk) and details["config_revision"] == 4
    assert "synthetic-secret" not in str(list(AuditEvent.objects.values("details")))


def test_idempotent_rollback_cannot_be_reused_as_activation_or_other_target(app, accounts, runtime):
    actor = accounts["publisher"]
    source, _ = history(app, actor)
    state, key = activation.rollback_state(app, source), uuid.uuid4()
    job = rollback(app, actor, source, key, state)
    finish(job)
    assert rollback(app, actor, source, key, state).pk == job.pk
    with pytest.raises(Conflict):
        activation.enqueue(actor, source.version_id, key)
    with pytest.raises(Conflict):
        rollback(app, accounts["admin"], source, key, state)
    assert AuditEvent.objects.filter(action="rollback.queued").count() == 1


@pytest.mark.parametrize("change", ["revision", "config_revision", "active_publication"])
def test_stale_confirmation_preserves_current_publication(app, accounts, runtime, change):
    actor = accounts["publisher"]
    source, current = history(app, actor)
    state = activation.rollback_state(app, source)
    state[change if change != "active_publication" else "active"] = "stale"
    with pytest.raises(Conflict, match="Reabra"):
        rollback(app, actor, source, state=state)
    app.refresh_from_db()
    assert app.active_publication_id == current.publication_id
    assert Publication.objects.count() == 2 and Job.objects.count() == 2


@pytest.mark.parametrize("status", ["active", "candidate", "failed", "cancelled", "retiring"])
def test_only_retired_publication_of_same_app_and_version_is_allowed(
    app, accounts, runtime, status
):
    actor = accounts["publisher"]
    source, _ = history(app, actor)
    source.status = status
    source.save(update_fields=["status"])
    with pytest.raises(ValidationError):
        rollback(app, actor, source)


def test_foreign_source_and_unprepared_artifact_are_refused(app, accounts, runtime):
    actor = accounts["publisher"]
    source, _ = history(app, actor)
    other_version = prepared(app, actor)
    with pytest.raises(ValidationError):
        activation.enqueue(
            actor,
            other_version.pk,
            uuid.uuid4(),
            restored_from=source.pk,
            expected_state=activation.rollback_state(app, source),
        )
    source.version.status = "build_failed"
    source.version.save(update_fields=["status"])
    with pytest.raises(ValidationError, match="artefato"):
        rollback(app, actor, source)


@pytest.mark.parametrize("failure", ["probe", "live", "permission", "cancel", "config"])
def test_rollback_failure_or_revocation_does_not_bypass_lifecycle(
    app, accounts, runtime, local_state, monkeypatch, failure
):
    actor = accounts["publisher"]
    enable_data(actor, app.pk, app.revision)
    source, previous = history(app, actor)
    job = rollback(app, actor, source)
    original = runtime_client.ensure

    def ensure(pub, *, probe=False):
        result = original(pub, probe=probe)
        if (failure == "probe" and probe) or (failure == "live" and not probe):
            result.update(state="failed", target="", error="application_readiness_failed")
        elif failure == "permission":
            actor.is_active = False
            actor.save()
        elif failure == "cancel":
            Job.objects.filter(pk=job.pk).update(cancel_requested=True)
        elif failure == "config":
            set_environment(actor, app.pk, 0, "LATE", "synthetic", False)
        return result

    monkeypatch.setattr(runtime_client, "ensure", ensure)
    finish(job)
    app.refresh_from_db()
    assert job.status in {"failed", "cancelled"}
    assert app.active_publication_id == (None if failure == "live" else previous.publication_id)
    if failure == "live":
        assert "volume foi mantido" in job.message
    else:
        assert ("remove", previous.publication_id) not in runtime[1]
    assert not AppInstance.objects.get(pk=job.publication_id).enabled


def test_history_permissions_pagination_no_secrets_and_confirmation(
    app, accounts, runtime, local_state, client
):
    actor = accounts["publisher"]
    set_environment(actor, app.pk, 0, "TOKEN", "history-secret-sentinel", True)
    source, _ = history(app, actor)
    urls = [
        reverse("app_history", args=[app.pk]),
        reverse("publication_rollback", args=[source.pk]),
    ]
    AppGrant.objects.create(app=app, user=accounts["viewer"], role="viewer")
    for account in ["viewer", "outsider"]:
        client.force_login(accounts[account])
        for url in urls:
            assert client.get(url).status_code == 404
        assert client.post(urls[1], {}).status_code == 404
        with pytest.raises(PermissionDenied):
            rollback(app, accounts[account], source)
    client.force_login(actor)
    for _ in range(22):
        prepared(app, actor)
    response = client.get(urls[0])
    assert len(response.context["versions"]) == 20
    assert len(client.get(urls[0] + "?versions=2").context["versions"]) == 4
    assert b"history-secret-sentinel" not in response.content
    response = client.get(urls[1])
    assert response.status_code == 200 and b"history-secret-sentinel" not in response.content
    form = response.context["form"]
    data = {
        "idempotency_key": str(form["idempotency_key"].value()),
        "confirmation": form["confirmation"].value(),
        "acknowledge": True,
    }
    assert client.post(urls[1], data).status_code == 200
    assert Job.objects.count() == 2
    data["acknowledge_data"] = True
    assert client.post(urls[1], data).status_code == 302
    assert Job.objects.count() == 3
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(actor)
    assert csrf_client.post(urls[1], data).status_code == 403


@pytest.mark.parametrize("kind", ["altered", "expired", "other_source"])
def test_invalid_or_retargeted_confirmation_has_no_effect(
    app, accounts, runtime, client, monkeypatch, kind
):
    actor = accounts["publisher"]
    source, _ = history(app, actor)
    client.force_login(actor)
    url = reverse("publication_rollback", args=[source.pk])
    state = activation.rollback_state(app, source)
    if kind == "other_source":
        state["source"] = str(uuid.uuid4())
    token = signing.dumps(state, salt=CONFIRMATION_SALT)
    if kind == "altered":
        token += "altered"
    if kind == "expired":
        monkeypatch.setattr(signing.time, "time", lambda: 9999999999)
    response = client.post(
        url,
        {
            "confirmation": token,
            "idempotency_key": uuid.uuid4(),
            "acknowledge": True,
            "acknowledge_data": True,
        },
    )
    assert response.status_code in {200, 409}
    assert Job.objects.count() == 2

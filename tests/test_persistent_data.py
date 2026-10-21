import copy
import uuid

import pytest
from django.core.exceptions import PermissionDenied
from django.urls import reverse

from control import activation_jobs as activation
from control import runtime_client
from control.models import AppInstance, AuditEvent, Job, Publication
from control.services import Conflict, enable_data, set_environment
from tests.test_activation import expire, prepared, ready

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def enabled(settings):
    settings.DEBUG = True
    settings.SHINYD_RUNTIME_ENABLED = True
    settings.SHINYD_PANEL_ORIGIN = "http://panel.localhost"
    settings.SHINYD_APP_DOMAIN = "apps.localhost"


@pytest.fixture
def runtime(monkeypatch):
    records, calls = {}, []

    def view(pub, probe):
        identity, spec = runtime_client.contract(pub, probe)
        value = {**ready(pub), **spec, "id": str(identity)}
        value["target"] = f"unix:/run/shinyd-runtime/apps/{identity}.sock"
        return identity, value

    def ensure(pub, *, probe=False):
        identity, value = view(pub, probe)
        calls.append(("start", identity))
        records[identity] = value
        return value

    def retire(pub, *, probe=False):
        identity, value = view(pub, probe)
        calls.append(("remove", identity))
        records[identity] = {**value, "state": "removed", "desired": "removed", "target": ""}
        return records[identity]

    monkeypatch.setattr(runtime_client, "ensure", ensure)
    monkeypatch.setattr(runtime_client, "retire", retire)
    monkeypatch.setattr(
        runtime_client,
        "inspect",
        lambda p, probe=False: records.get(runtime_client.contract(p, probe)[0]),
    )
    return records, calls


def queue(app, actor):
    return activation.enqueue(actor, prepared(app, actor).pk, uuid.uuid4())


def finish(job):
    for _ in range(10):
        expire(job)
        activation.process_one()
        job.refresh_from_db()
        if job.status not in {"running", "queued"}:
            return
    pytest.fail("job never finished")


def test_enable_requires_permission_revision_confirmation_and_idle_job(
    app, accounts, client, runtime
):
    actor = accounts["publisher"]
    url = reverse("data_enable", args=[app.pk])
    with pytest.raises(PermissionDenied):
        enable_data(accounts["viewer"], app.pk, app.revision)
    client.force_login(accounts["viewer"])
    assert client.post(url, {"revision": app.revision, "acknowledge": True}).status_code == 404
    client.force_login(actor)
    assert client.post(url, {"revision": app.revision}).status_code == 200
    app.refresh_from_db()
    assert not app.data_enabled
    assert client.post(url, {"revision": 999, "acknowledge": True}).status_code == 409
    job = queue(app, actor)
    with pytest.raises(Conflict):
        enable_data(actor, app.pk, app.revision)
    finish(job)
    assert client.post(url, {"revision": app.revision, "acknowledge": True}).status_code == 302
    app.refresh_from_db()
    assert app.data_enabled
    assert AuditEvent.objects.get(action="data.enabled").details == {}
    # Same code/configuration can now be activated to apply persistence.
    job = activation.enqueue(actor, job.version_id, uuid.uuid4())
    assert job.publication.runtime_spec["data_mode"] == "persistent"


def test_persistent_switch_is_staged_and_previous_writer_removed_first(app, accounts, runtime):
    actor = accounts["publisher"]
    enable_data(actor, app.pk, app.revision)
    first = queue(app, actor)
    finish(first)
    second = queue(app, actor)
    probe = runtime_client.probe_id(second.publication)
    activation.process_one()
    app.refresh_from_db()
    assert app.active_publication_id == first.publication_id
    assert AppInstance.objects.get(pk=first.publication_id).enabled
    expire(second)
    activation.process_one()
    app.refresh_from_db()
    assert app.active_publication_id is None
    assert not AppInstance.objects.get(pk=first.publication_id).enabled
    assert ("start", second.publication_id) not in runtime[1]
    finish(second)
    assert second.status == "succeeded"
    calls = runtime[1]
    assert calls.index(("start", probe)) < calls.index(("remove", probe))
    assert calls.index(("remove", probe)) < calls.index(("remove", first.publication_id))
    assert calls.index(("remove", first.publication_id)) < calls.index(
        ("start", second.publication_id)
    )
    assert Publication.objects.get(pk=first.publication_id).status == "retired"


@pytest.mark.parametrize("after_cutover", [False, True])
def test_cancellation_handles_both_sides_of_cutover(app, accounts, runtime, after_cutover):
    actor = accounts["publisher"]
    enable_data(actor, app.pk, app.revision)
    first = queue(app, actor)
    finish(first)
    second = queue(app, actor)
    activation.process_one()
    if after_cutover:
        expire(second)
        activation.process_one()
    Job.objects.filter(pk=second.pk).update(cancel_requested=True)
    finish(second)
    app.refresh_from_db()
    assert second.status == "cancelled"
    assert app.active_publication_id == (None if after_cutover else first.publication_id)
    assert ("start", second.publication_id) not in runtime[1]
    assert ("remove", runtime_client.probe_id(second.publication)) in runtime[1]
    assert ("remove", second.publication_id) in runtime[1]
    if after_cutover:
        assert "foi preservada" not in second.message and "volume foi mantido" in second.message
        assert ("remove", first.publication_id) in runtime[1]


@pytest.mark.parametrize("probe_failure", [False, True])
def test_failed_probe_preserves_previous_but_live_failure_keeps_app_offline(
    app, accounts, runtime, monkeypatch, probe_failure
):
    actor = accounts["publisher"]
    enable_data(actor, app.pk, app.revision)
    first = queue(app, actor)
    finish(first)
    original = runtime_client.ensure

    def failed(pub, *, probe=False):
        result = original(pub, probe=probe)
        if probe == probe_failure:
            result.update(state="failed", target="", error="application_readiness_failed")
        return result

    monkeypatch.setattr(runtime_client, "ensure", failed)
    second = queue(app, actor)
    finish(second)
    app.refresh_from_db()
    assert second.status == "failed"
    assert app.active_publication_id == (first.publication_id if probe_failure else None)
    if probe_failure:
        assert ("start", second.publication_id) not in runtime[1]


def test_lost_probe_cleanup_response_resumes_without_restarting_probe(
    app, accounts, runtime, monkeypatch
):
    actor = accounts["publisher"]
    enable_data(actor, app.pk, app.revision)
    job = queue(app, actor)
    original = runtime_client.retire
    lost = [False]

    def retire(pub, *, probe=False):
        result = original(pub, probe=probe)
        if probe and not lost[0]:
            lost[0] = True
            raise runtime_client.RuntimeUnavailable
        return result

    monkeypatch.setattr(runtime_client, "retire", retire)
    finish(job)
    assert job.status == "succeeded"
    assert runtime[1].count(("start", runtime_client.probe_id(job.publication))) == 1
    assert runtime[1].count(("start", job.publication_id)) == 1


def test_retirement_failure_blocks_live_writer(app, accounts, runtime, monkeypatch):
    actor = accounts["publisher"]
    enable_data(actor, app.pk, app.revision)
    first = queue(app, actor)
    finish(first)
    second = queue(app, actor)
    original = runtime_client.retire

    def retire(pub, *, probe=False):
        if pub.pk == first.publication_id:
            raise runtime_client.RuntimeUnavailable
        return original(pub, probe=probe)

    monkeypatch.setattr(runtime_client, "retire", retire)
    for _ in range(5):
        expire(second)
        activation.process_one()
    assert ("start", second.publication_id) not in runtime[1]
    monkeypatch.setattr(runtime_client, "retire", original)
    finish(second)
    assert second.status == "succeeded"


def test_probe_keeps_original_encrypted_snapshot_identity(app, accounts, local_state, monkeypatch):
    actor = accounts["publisher"]
    enable_data(actor, app.pk, app.revision)
    set_environment(actor, app.pk, 0, "TOKEN", "synthetic-private", True)
    pub = queue(app, actor).publication
    calls = []
    monkeypatch.setattr(runtime_client, "request", lambda *a, **k: calls.append((a, k)))
    original = copy.deepcopy(pub.runtime_spec)
    runtime_client.ensure(pub, probe=True)
    assert calls[0][0][1] == runtime_client.probe_id(pub)
    assert calls[0][0][2]["data_mode"] == "scratch"
    assert calls[0][1]["environment"] == {"TOKEN": "synthetic-private"}
    assert pub.runtime_spec == original


def test_stale_worker_cannot_cutover_or_checkpoint(app, accounts, runtime):
    actor = accounts["publisher"]
    enable_data(actor, app.pk, app.revision)
    queue(app, actor)
    old = activation.claim()
    expire(old)
    current = activation.claim()
    assert not activation.data_checkpoint(old, preflight=True)
    assert activation.data_checkpoint(current, preflight=True)
    assert not activation.data_checkpoint(old, cutover=True)


def test_persistent_publication_cannot_skip_probe_and_writer_retirement(app, accounts, runtime):
    actor = accounts["publisher"]
    enable_data(actor, app.pk, app.revision)
    job = queue(app, actor)
    claimed = activation.claim()
    assert not activation.decide(claimed, ready(job.publication))
    assert not activation.data_checkpoint(claimed, cutover=True)
    app.refresh_from_db()
    assert app.active_publication_id is None


def test_configuration_changed_after_probe_preserves_old_app(app, accounts, runtime, local_state):
    actor = accounts["publisher"]
    enable_data(actor, app.pk, app.revision)
    first = queue(app, actor)
    finish(first)
    second = queue(app, actor)
    activation.process_one()
    set_environment(actor, app.pk, 0, "NEW_VALUE", "synthetic", False)
    finish(second)
    app.refresh_from_db()
    assert second.status == "failed" and app.active_publication_id == first.publication_id
    assert ("remove", first.publication_id) not in runtime[1]

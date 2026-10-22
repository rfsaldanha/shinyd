from datetime import timedelta

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from control import lifecycle, recovery, runtime_client
from control.models import AppInstance, Application, AuditEvent, Job, RecoveryState
from control.services import Conflict, enable_data, set_environment
from tests import test_persistent_data as persistent
from tests.test_lifecycle import operation, publish

pytestmark = pytest.mark.django_db
runtime = persistent.runtime
enabled = persistent.enabled


def setup(app, accounts):
    pub = publish(app, accounts["publisher"])
    row = recovery.configure(accounts["publisher"], app.pk, 0, True)
    return pub, row


def tick(row, *, due=False):
    RecoveryState.objects.filter(pk=row.pk).update(next_check_at=timezone.now())
    if due:
        RecoveryState.objects.filter(pk=row.pk).update(next_attempt_at=timezone.now())
    assert recovery.process_one()
    row.refresh_from_db()


def crash(runtime, pub, code="process_not_running"):
    runtime[0][pub.pk].update(state="failed", error=code, target="")


def test_recovery_preserves_code_config_other_app_and_stops_after_three(app, accounts, runtime):
    pub, row = setup(app, accounts)
    other = Application.objects.create(
        owner=app.owner, priority=app.priority, name="Other", slug="other"
    )
    other_pub = publish(other, accounts["publisher"])
    # Keep the other monitor outside this test's forced ticks.
    RecoveryState.objects.filter(app=other).update(
        next_check_at=timezone.now() + timedelta(hours=1)
    )
    version = pub.version_id
    for number, delay in enumerate([10, 30, 60], 1):
        crash(runtime, pub)
        tick(row)
        assert row.state == "waiting" and not row.job_id
        assert delay - 1 <= (row.next_attempt_at - timezone.now()).total_seconds() <= delay
        assert not AppInstance.objects.get(pk=pub.pk).enabled
        tick(row, due=True)
        assert row.state == "recovering" and row.attempts == number
        job = row.job
        assert job.operation == "recover" and job.version_id == version
        assert job.publication_id != pub.pk
        persistent.finish(job)
        assert job.status == "succeeded"
        tick(row)
        app.refresh_from_db()
        pub = app.active_publication
        tick(row)
        assert row.state == "healthy" and row.attempts == number
    crash(runtime, pub)
    tick(row)
    assert row.state == "open" and row.next_attempt_at is None
    assert Job.objects.filter(operation="recover").count() == 3
    assert ("remove", other_pub.pk) not in runtime[1]
    # A new claim/worker cannot forget the circuit.
    assert recovery.claim() is None
    assert AuditEvent.objects.filter(action="recovery.open").count() == 1


@pytest.mark.parametrize(
    "kind", ["missing", "unavailable", "unknown", "oom", "disabled", "permission", "config"]
)
def test_uncertainty_and_policy_preconditions_never_launch(
    app, accounts, runtime, monkeypatch, local_state, kind
):
    pub, row = setup(app, accounts)
    crash(runtime, pub)
    expected = "unknown"
    if kind == "missing":
        del runtime[0][pub.pk]
    elif kind == "unavailable":

        def unavailable(*args, **kwargs):
            raise runtime_client.RuntimeUnavailable

        monkeypatch.setattr(runtime_client, "inspect", unavailable)
    elif kind == "unknown":
        runtime[0][pub.pk]["error"] = "runtime_unavailable"
    elif kind == "oom":
        runtime[0][pub.pk]["error"] = "process_oom"
        expected = "open"
    elif kind == "disabled":
        row = recovery.configure(accounts["publisher"], app.pk, row.revision, False)
        expected = "failed"
    elif kind == "permission":
        accounts["publisher"].is_active = False
        accounts["publisher"].save()
        expected = "paused"
    else:
        set_environment(accounts["publisher"], app.pk, 0, "TOKEN", "pending-secret", True)
        expected = "paused"
    tick(row, due=True)
    assert row.state == expected and row.attempts == 0 and row.job_id is None
    assert not Job.objects.filter(operation="recover").exists()
    assert "pending-secret" not in str(list(AuditEvent.objects.values()))


def test_manual_stop_during_backoff_and_stale_monitor_cannot_start(app, accounts, runtime):
    pub, row = setup(app, accounts)
    crash(runtime, pub)
    tick(row)
    RecoveryState.objects.filter(pk=row.pk).update(
        next_check_at=timezone.now(), next_attempt_at=timezone.now()
    )
    old_claim = recovery.claim()
    stopped = operation(app, accounts["publisher"], "stop")
    lifecycle.process_one()
    stopped.refresh_from_db()
    assert stopped.status == "succeeded"
    recovery.accept(old_claim, pub.pk, runtime[0][pub.pk])
    assert not Job.objects.filter(operation="recover").exists()
    app.refresh_from_db()
    assert app.desired_state == "stopped" and not app.active_publication_id
    assert recovery.claim() is None


def test_disable_cancels_pending_recovery_and_keeps_cleanup(app, accounts, runtime):
    pub, row = setup(app, accounts)
    crash(runtime, pub)
    tick(row, due=True)
    job = row.job
    recovery.configure(accounts["publisher"], app.pk, row.revision, False)
    persistent.finish(job)
    assert job.status == "cancelled"
    assert ("remove", job.publication_id) in runtime[1]
    tick(row)
    assert row.state == "paused" and not row.enabled
    assert Job.objects.filter(operation="recover").count() == 1


def test_persistent_recovery_failure_after_cutover_retries_bounded(
    app, accounts, runtime, monkeypatch
):
    actor = accounts["publisher"]
    enable_data(actor, app.pk, app.revision)
    pub, row = setup(app, accounts)
    crash(runtime, pub)
    original = runtime_client.ensure

    def failed(pub, *, probe=False):
        value = original(pub, probe=probe)
        if not probe:
            value.update(state="failed", error="application_readiness_failed", target="")
        return value

    monkeypatch.setattr(runtime_client, "ensure", failed)
    for attempt in range(1, 4):
        tick(row, due=True)
        job = row.job
        assert job and row.attempts == attempt
        persistent.finish(job)
        assert job.status == "failed"
        app.refresh_from_db()
        assert app.active_publication_id is None and app.desired_state == "running"
        tick(row)
    assert row.state == "open" and row.attempts == 3
    assert runtime[1].index(("remove", pub.pk)) < runtime[1].index(
        ("start", Job.objects.filter(operation="recover").first().publication_id)
    )
    assert app.last_version_id == pub.version_id and app.data_enabled
    stopped = operation(app, actor, "stop")
    assert stopped is None  # No active writer, but stop cancels the desired running state.
    app.refresh_from_db()
    assert app.desired_state == "stopped"


def test_queue_full_waits_without_spending_attempt(app, accounts, runtime):
    pub, row = setup(app, accounts)
    crash(runtime, pub)
    Job.objects.bulk_create(
        [Job(app=app, actor=app.owner, kind="inspect_upload") for _ in range(100)]
    )
    tick(row, due=True)
    assert row.state == "waiting" and row.attempts == 0
    assert "fila" in row.message and row.job_id is None


def test_stable_observation_resets_sequence_but_not_on_missing_telemetry(app, accounts, runtime):
    pub, row = setup(app, accounts)
    tick(row)
    now = timezone.now()
    RecoveryState.objects.filter(pk=row.pk).update(
        attempts=2,
        healthy_since=now - timedelta(minutes=11),
        checked_at=now - timedelta(seconds=10),
    )
    tick(row)
    assert row.attempts == 0
    RecoveryState.objects.filter(pk=row.pk).update(
        attempts=2, healthy_since=now - timedelta(minutes=11), checked_at=now - timedelta(minutes=2)
    )
    tick(row)
    assert row.attempts == 2 and row.healthy_since > now


def test_rearming_does_not_bypass_rolling_limit(app, accounts, runtime):
    pub, row = setup(app, accounts)
    Job.objects.bulk_create(
        [
            Job(
                app=app,
                actor=app.owner,
                kind="activate_version",
                operation="recover",
                status="failed",
            )
            for _ in range(3)
        ]
    )
    row = recovery.configure(app.owner, app.pk, row.revision, True)
    crash(runtime, pub)
    tick(row, due=True)
    assert row.state == "open" and row.attempts == 0


def test_policy_form_permissions_csrf_stale_confirmation_and_mobile_content(
    app, accounts, runtime, client
):
    publish(app, accounts["publisher"])
    url = reverse("app_recovery", args=[app.pk])
    client.force_login(accounts["viewer"])
    assert client.get(url).status_code == 404
    client.force_login(accounts["publisher"])
    response = client.get(url)
    token = response.context["form"].initial["confirmation"]
    payload = {"confirmation": token, "enabled": True, "acknowledge": True}
    assert client.post(url, {**payload, "acknowledge": ""}).status_code == 200
    csrf = Client(enforce_csrf_checks=True)
    csrf.force_login(accounts["publisher"])
    assert csrf.post(url, payload).status_code == 403
    assert client.post(url, payload).status_code == 302
    assert client.post(url, payload).status_code == 409
    page = client.get(reverse("app_detail", args=[app.pk]))
    assert "Recuperação de falhas" in page.content.decode()
    assert "Nenhuma" not in RecoveryState.objects.get(app=app).message
    with pytest.raises(Conflict):
        recovery.configure(accounts["publisher"], app.pk, 0, True)


def test_only_one_monitor_lease_and_stale_lease_cannot_schedule(app, accounts, runtime):
    pub, row = setup(app, accounts)
    crash(runtime, pub)
    first = recovery.claim()
    assert first and recovery.claim() is None
    RecoveryState.objects.filter(pk=row.pk).update(
        next_check_at=timezone.now(), next_attempt_at=timezone.now()
    )
    second = recovery.claim()
    recovery.accept(first, pub.pk, runtime[0][pub.pk])
    assert not Job.objects.filter(operation="recover").exists()
    recovery.accept(second, pub.pk, runtime[0][pub.pk])
    assert Job.objects.filter(operation="recover").count() == 1


@pytest.mark.parametrize("persistent_volume", [False, True])
def test_oom_in_candidate_stops_recovery_after_confirmed_cleanup(
    app, accounts, runtime, monkeypatch, persistent_volume
):
    if persistent_volume:
        enable_data(accounts["publisher"], app.pk, app.revision)
    pub, row = setup(app, accounts)
    crash(runtime, pub)
    tick(row, due=True)
    job = row.job
    original = runtime_client.ensure

    def oom(pub, *, probe=False):
        value = original(pub, probe=probe)
        value.update(state="failed", error="process_oom", target="")
        return value

    monkeypatch.setattr(runtime_client, "ensure", oom)
    persistent.finish(job)
    assert job.status == "failed"
    assert ("remove", job.publication_id) in runtime[1]
    tick(row)
    assert row.state == "open" and row.attempts == 1 and not row.job_id
    assert recovery.claim() is None


def test_runtime_disabled_and_manual_job_take_precedence(app, accounts, runtime, settings):
    pub, row = setup(app, accounts)
    crash(runtime, pub)
    settings.SHINYD_RUNTIME_ENABLED = False
    assert recovery.process_one() is False
    settings.SHINYD_RUNTIME_ENABLED = True
    job = operation(app, accounts["publisher"], "restart")
    tick(row, due=True)
    assert not Job.objects.filter(operation="recover").exists()
    persistent.finish(job)
    tick(row)
    assert row.state == "healthy" and row.attempts == 0

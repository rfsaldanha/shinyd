"""Two monitor connections must not both spend a recovery attempt."""

import os
import subprocess
import sys
from pathlib import Path


def test_recovery_is_serialized_with_production_sqlite(tmp_path):
    source = r"""
import threading, uuid
from concurrent.futures import ThreadPoolExecutor
import django
django.setup()
from django.conf import settings
from django.core.management import call_command
from django.db import connections
from django.utils import timezone
from control import activation_jobs as activation, recovery
from control.models import Application, Job, PriorityGroup, RecoveryState, User
from tests.test_activation import prepared, ready
call_command("migrate", verbosity=0)
settings.SHINYD_RUNTIME_ENABLED = True
settings.SHINYD_PANEL_ORIGIN = "http://panel.localhost"
settings.SHINYD_APP_DOMAIN = "apps.localhost"
assert connections["default"].settings_dict["OPTIONS"]["transaction_mode"] == "IMMEDIATE"
actor = User.objects.create_user("recovery-actor", can_publish=True)
app = Application.objects.create(name="race", slug="race", owner=actor,
    priority=PriorityGroup.objects.get(slug="normal"))
job = activation.enqueue(actor, prepared(app, actor).pk, uuid.uuid4())
claimed = activation.claim()
assert activation.decide(claimed, ready(job.publication))
activation.update(claimed, phase="active", status="succeeded", message="fixture")
row = recovery.configure(actor, app.pk, 0, True)
RecoveryState.objects.filter(pk=app.pk).update(next_attempt_at=timezone.now())
barrier = threading.Barrier(2)
result = {**ready(job.publication), "state":"failed", "error":"process_not_running", "target":""}
def monitor():
    try:
        barrier.wait(timeout=5)
        claim = recovery.claim()
        if claim:
            recovery.accept(claim, job.publication_id, result)
        return claim is not None
    finally:
        connections.close_all()
with ThreadPoolExecutor(max_workers=2) as pool:
    answers = list(pool.map(lambda _: monitor(), range(2)))
assert sorted(answers) == [False, True]
assert Job.objects.filter(operation="recover").count() == 1
assert RecoveryState.objects.get(pk=app.pk).attempts == 1
print("single-recovery-with-two-workers")
"""
    result = subprocess.run(
        [sys.executable, "-c", source],
        cwd=Path(__file__).resolve().parents[1],
        env={
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "shinyd_web.settings",
            "PYTHONPATH": "backend:.",
            "SHINYD_DEV": "1",
            "SHINYD_STATE_DIR": str(tmp_path),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "single-recovery-with-two-workers" in result.stdout

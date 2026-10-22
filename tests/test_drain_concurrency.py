"""Exercise the production SQLite transaction mode using two real connections."""

import os
import subprocess
import sys
from pathlib import Path


def test_admission_and_cutover_are_serialized_in_both_orders(tmp_path):
    source = r"""
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError
import django
django.setup()
from django.conf import settings
from django.core.management import call_command
from django.db import connections as databases, transaction
from control import activation_jobs as activation, connections
from control.models import AppInstance, Application, Publication, PriorityGroup, User
from tests.test_activation import prepared, ready

call_command("migrate", verbosity=0)
settings.SHINYD_RUNTIME_ENABLED = True
settings.SHINYD_PANEL_ORIGIN = "http://panel.localhost"
settings.SHINYD_APP_DOMAIN = "apps.localhost"
actor = User.objects.create_user("publisher", can_publish=True)
priority = PriorityGroup.objects.get(slug="normal")
assert databases["default"].settings_dict["OPTIONS"]["transaction_mode"] == "IMMEDIATE"

for admission_first in [True, False]:
    app = Application.objects.create(name="race", slug="race-"+str(int(admission_first)),
        owner=actor, priority=priority, visibility="public")
    old = Publication.objects.create(app=app, version=prepared(app, actor), created_by=actor,
        config_revision=0, status="active")
    app.active_publication = old
    app.save()
    instance = AppInstance.objects.create(id=old.pk, app=app, publication=old,
        host="p"+old.pk.hex+".apps.localhost", target="http://127.0.0.1:3838", enabled=True)
    queued = activation.enqueue(actor, prepared(app, actor).pk, uuid.uuid4(), switch_mode="drain")
    job = activation.claim()
    assert job.pk == queued.pk
    activity = dict(operation="open", request_id=str(uuid.uuid4()), host=instance.host,
        credential="", kind="websocket")
    admitted = lambda: connections.track(**activity)
    cutover = lambda: activation.decide(job, ready(queued.publication))
    entered = threading.Event()
    def contender():
        try:
            entered.set()
            return cutover() if admission_first else admitted()
        finally:
            databases.close_all()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            result = admitted() if admission_first else cutover()
            assert result["allowed"] if admission_first else result
            future = pool.submit(contender)
            assert entered.wait(5)
            try:
                future.result(timeout=0.1)
            except TimeoutError:
                pass
            else:
                raise AssertionError("contender bypassed uncommitted writer")
        result = future.result(timeout=10)
    app.refresh_from_db()
    if admission_first:
        assert result is False
        assert app.active_publication_id == old.pk
        assert connections.counts(old.pk)["opening"] == 1
    else:
        assert result == {"allowed": False, "reason": "not_found"}
        assert app.active_publication_id == queued.publication_id
        assert connections.counts(old.pk)["total"] == 0
print("admission-and-cutover-serialized")
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
    assert "admission-and-cutover-serialized" in result.stdout

import io
import json
import os
import uuid
from datetime import timedelta

import pytest
from cryptography.exceptions import InvalidTag
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse
from django.utils import timezone

from control import activation_jobs, runtime_client
from control.environment import reveal, validate
from control.models import AuditEvent, EnvironmentValue, Job, Version
from control.services import Conflict, remove_environment, set_environment

pytestmark = pytest.mark.django_db


@pytest.fixture
def version(app, accounts, local_state, settings):
    settings.SHINYD_RUNTIME_ENABLED = True
    settings.DEBUG = True
    settings.SHINYD_PANEL_ORIGIN = "http://panel.localhost"
    settings.SHINYD_APP_DOMAIN = "apps.localhost"
    return Version.objects.create(
        app=app,
        created_by=accounts["publisher"],
        status="prepared",
        source_hash="a" * 64,
        image_digest="sha256:" + "b" * 64,
        build_manifest={
            "app_id": str(app.pk),
            "source_hash": "a" * 64,
            "image": "sha256:" + "b" * 64,
            "cache_key": "c" * 64,
        },
    )


def queue(version, actor):
    return activation_jobs.enqueue(actor, version.pk, uuid.uuid4())


def test_snapshot_is_immutable_encrypted_and_bound_to_publication(version, app, accounts):
    actor = accounts["publisher"]
    secret = 'line one\n"ç"=two\\$(no-execution)'
    set_environment(actor, app.pk, 0, "TOKEN", secret, True)
    first = queue(version, actor).publication
    assert reveal(first) == {"TOKEN": secret}
    assert secret.encode() not in bytes(first.environment_snapshot)
    set_environment(actor, app.pk, 1, "TOKEN", "replacement", True)
    assert reveal(first) == {"TOKEN": secret}
    original = first.pk
    first.pk = uuid.uuid4()
    with pytest.raises(InvalidTag):
        reveal(first)
    first.pk = original
    first.config_revision += 1
    with pytest.raises(InvalidTag):
        reveal(first)


def test_configuration_applies_without_upload_and_values_use_only_put(
    version, app, accounts, monkeypatch, client
):
    actor = accounts["publisher"]
    calls, values = [], {}

    class Connection:
        def __init__(self, path, uid):
            assert uid == 0

        def request(self, method, path, body, headers):
            self.method, self.path = method, path
            self.payload = json.loads(body) if body else None
            calls.append((method, self.payload))

        def getresponse(self):
            identity = self.path.split("/")[3]
            if self.method == "PUT":
                value = {k: v for k, v in self.payload.items() if k != "environment"}
                values[identity] = {
                    **value,
                    "id": identity,
                    "image": version.image_digest,
                    "state": "ready",
                    "desired": "running",
                    "target": f"unix:/run/shinyd-runtime/apps/{identity}.sock",
                }
            elif self.method == "POST":
                values[identity] = {
                    **self.payload,
                    "id": identity,
                    "state": "removed",
                    "desired": "removed",
                }
            result = io.BytesIO(json.dumps(values.get(identity, {})).encode())
            result.status = 200 if identity in values else 404
            return result

        def close(self):
            pass

    monkeypatch.setattr(runtime_client, "UnixConnection", Connection)
    set_environment(actor, app.pk, 0, "TOKEN", "first-secret", True)
    first = queue(version, actor)
    activation_jobs.process_one()
    first.refresh_from_db()
    assert first.status == "succeeded"
    set_environment(actor, app.pk, 1, "TOKEN", "new-secret", True)
    client.force_login(actor)
    page = client.get(reverse("app_detail", args=[app.pk]))
    assert b"Aplicar configura" in page.content and b"new-secret" not in page.content
    second = queue(version, actor)
    activation_jobs.process_one()
    second.refresh_from_db()
    app.refresh_from_db()
    assert second.status == "succeeded" and app.active_publication_id == second.publication_id
    assert Version.objects.count() == 1 and first.publication_id != second.publication_id
    assert [body["environment"] for method, body in calls if method == "PUT"] == [
        {"TOKEN": "first-secret"},
        {"TOKEN": "new-secret"},
    ]
    assert all(
        body is None or "environment" not in body for method, body in calls if method != "PUT"
    )
    assert all("secret" not in json.dumps(row) for row in AuditEvent.objects.values("details"))
    with pytest.raises(Conflict):
        queue(version, actor)
    remove_environment(actor, app.pk, 2, "TOKEN")
    third = queue(version, actor)
    assert reveal(third.publication) == {}
    activation_jobs.process_one()
    third.refresh_from_db()
    assert third.status == "succeeded"


@pytest.mark.parametrize("problem", ["key", "ciphertext", "configuration_changed"])
def test_unreadable_or_stale_snapshot_never_starts(
    version, app, accounts, settings, monkeypatch, problem
):
    set_environment(accounts["publisher"], app.pk, 0, "TOKEN", "private-sentinel", True)
    job = queue(version, accounts["publisher"])
    if problem == "key":
        settings.SHINYD_MASTER_KEY_FILE.write_bytes(os.urandom(32))
    elif problem == "ciphertext":
        pub = job.publication
        pub.environment_snapshot = b"wrong"
        pub.save()
    else:
        set_environment(accounts["publisher"], app.pk, 1, "TOKEN", "changed", True)
    monkeypatch.setattr(runtime_client, "inspect", lambda pub: None)
    retired = []
    monkeypatch.setattr(runtime_client, "retire", lambda pub: retired.append(pub.pk))
    # A real socket request would fail here; the candidate must fail before connecting.
    monkeypatch.setattr(
        runtime_client, "request", lambda *a, **k: pytest.fail("sent invalid environment")
    )
    activation_jobs.process_one()
    Job.objects.filter(pk=job.pk).update(lease_until=timezone.now() - timedelta(seconds=1))
    activation_jobs.process_one()
    job.refresh_from_db()
    assert job.status == "failed" and retired == [job.publication_id]
    assert "private-sentinel" not in job.message


def test_remove_requires_publish_and_revision_and_does_not_render_secret(
    client, app, accounts, local_state
):
    actor = accounts["publisher"]
    set_environment(actor, app.pk, 0, "TOKEN", "sentinel-private", True)
    url = reverse("environment_remove", args=[app.pk, "TOKEN"])
    with pytest.raises(PermissionDenied):
        remove_environment(accounts["viewer"], app.pk, 1, "TOKEN")
    client.force_login(accounts["outsider"])
    assert client.get(url).status_code == 404
    client.force_login(actor)
    assert b"sentinel-private" not in client.get(url).content
    assert EnvironmentValue.objects.count() == 1
    assert client.post(url, {"revision": 0, "acknowledge": True}).status_code == 409
    assert client.post(url, {"revision": 1}).status_code == 200
    assert client.post(url, {"revision": 1, "acknowledge": True}).status_code == 302
    assert not EnvironmentValue.objects.exists()
    app.refresh_from_db()
    assert app.config_revision == 2
    assert AuditEvent.objects.get(action="environment.removed").details == {
        "name": "TOKEN",
        "revision": 2,
    }


@pytest.mark.parametrize(
    "values",
    [
        {"PORT": "9"},
        {"RHOME": "/tmp"},
        {"RENV_PATHS_LIBRARY": "x"},
        {" TOKEN": "x"},
        {"TOKEN": "\x00"},
        {"TOKEN": "ç" * 8193},
        {"TOKEN": 5},
        {f"KEY{i}": "" for i in range(65)},
        {f"KEY{i}": "x" * 16384 for i in range(4)},
    ],
)
def test_environment_bounds(values):
    with pytest.raises(ValidationError):
        validate(values)


def test_empty_and_multiline_environment_are_valid():
    validate({"TOKEN": "", "CERTIFICATE": "one\r\nç\n\t='\"\\two"})


def test_combined_limit_has_safe_actionable_message(client, app, accounts, local_state):
    actor = accounts["publisher"]
    for i in range(4):
        set_environment(actor, app.pk, i, f"KEY{i}", "x" * 15360, True)
    client.force_login(actor)
    secret = "SECRET-SENTINEL" * 400
    response = client.post(
        reverse("app_config", args=[app.pk]),
        {
            "revision": 4,
            "name": "LAST_TOKEN",
            "value": secret,
            "is_secret": True,
        },
    )
    assert response.status_code == 400 and b"64 KiB" in response.content
    assert secret.encode() not in response.content
    app.refresh_from_db()
    assert app.config_revision == 4 and EnvironmentValue.objects.count() == 4

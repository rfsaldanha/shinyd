import uuid
from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from control.jobs import claim, finish, process_one
from control.models import Job, Version
from control.services import Conflict, receive_upload
from tests.test_archives import archive_bytes

pytestmark = pytest.mark.django_db


def send(app, actor, key=None, code="shiny::shinyApp(ui, server)", *, payload=None):
    upload = SimpleUploadedFile(
        "app.zip", archive_bytes([("app.R", code)]) if payload is None else payload
    )
    return receive_upload(actor, app.pk, upload, key or uuid.uuid4())


def test_upload_is_idempotent_and_no_execution(app, accounts, local_state):
    key = uuid.uuid4()
    # A retry sends the same ZIP bytes, including the original entry timestamps.
    payload = archive_bytes([("app.R", "shiny::shinyApp(ui, server)")])
    job = send(app, accounts["publisher"], key, payload=payload)
    assert send(app, accounts["publisher"], key, payload=payload).pk == job.pk
    assert Version.objects.count() == 1
    assert len(list((local_state / "sources").glob("*.zip"))) == 1
    assert process_one()
    job.refresh_from_db()
    assert job.status == "succeeded"
    assert job.phase == "validated"
    assert job.version.status == "validated"
    app.refresh_from_db()
    assert app.active_publication is None
    assert not list((local_state / "staging").iterdir())


def test_idempotency_conflict_with_different_bytes(app, accounts, local_state):
    key = uuid.uuid4()
    send(app, accounts["publisher"], key)
    with pytest.raises(Conflict):
        send(app, accounts["publisher"], key, code="different")
    assert Version.objects.count() == 1
    assert len(list((local_state / "sources").glob("*.zip"))) == 1


def test_worker_invalid_archive_fails_with_friendly_message(app, accounts, local_state):
    job = receive_upload(
        accounts["publisher"], app.pk, SimpleUploadedFile("app.zip", b"not a zip"), uuid.uuid4()
    )
    process_one()
    job.refresh_from_db()
    assert job.status == "failed"
    assert "ZIP" in job.message
    assert app.active_publication is None


def test_expired_lease_can_retry_but_stale_worker_cannot_finish(app, accounts, local_state):
    send(app, accounts["publisher"])
    old = claim()
    assert claim() is None
    Job.objects.filter(pk=old.pk).update(lease_until=timezone.now() - timedelta(seconds=1))
    new = claim()
    assert new.lease_token != old.lease_token
    assert not finish(old, error="stale error")
    assert finish(new, error="current error")
    new.refresh_from_db()
    assert new.message == "current error"


def test_cancel_and_object_permission(client, app, accounts, local_state):
    job = send(app, accounts["publisher"])
    client.force_login(accounts["outsider"])
    assert client.get(reverse("api_job", args=[job.pk])).status_code == 404
    assert client.post(reverse("job_cancel", args=[job.pk])).status_code == 404
    client.force_login(accounts["publisher"])
    assert client.post(reverse("job_cancel", args=[job.pk])).status_code == 302
    assert not process_one()
    job.refresh_from_db()
    assert job.status == "cancelled"


def test_zip_upload_http_and_job_page(client, app, accounts, local_state):
    client.force_login(accounts["publisher"])
    response = client.post(
        reverse("upload", args=[app.pk]),
        {
            "archive": SimpleUploadedFile("app.zip", archive_bytes([("app.R", "code")])),
            "idempotency_key": str(uuid.uuid4()),
        },
    )
    assert response.status_code == 302
    assert client.get(response["Location"]).status_code == 200
    process_one()
    assert b"ZIP validado" in client.get(response["Location"]).content

#!/usr/bin/env python3
"""Exercise Django views/worker against the installed API as shinyd-control."""

import argparse
import json
import os
import subprocess
import sys
import time
import traceback
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--stage", choices=["submit", "complete"], required=True)
    args = parser.parse_args()
    if os.geteuid() == 0:
        raise RuntimeError("Run as the control service account.")
    os.environ.update(
        {
            "SHINYD_DEV": "1",
            "SHINYD_STATE_DIR": str(args.state),
            "SHINYD_ALLOWED_HOSTS": "testserver",
            "DJANGO_SETTINGS_MODULE": "shinyd_web.settings",
        }
    )
    sys.path.insert(0, str(ROOT / "backend"))
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/bootstrap.py"), "--state-dir", str(args.state)],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    import django

    django.setup()
    from django.core.files.uploadedfile import SimpleUploadedFile
    from django.core.management import call_command
    from django.test import Client
    from django.urls import reverse

    from control.jobs import process_one
    from control.models import Application, Job, PriorityGroup, User
    from control.services import set_environment

    if args.stage == "submit":
        call_command("migrate", verbosity=0)
        actor = User.objects.create_user("publisher", can_publish=True)
        app = Application.objects.create(
            name="Builder panel fixture",
            slug="builder-fixture",
            owner=actor,
            priority=PriorityGroup.objects.get(slug="normal"),
        )
        set_environment(actor, app.pk, 0, "TEST_TOKEN", "PRIVATE_PANEL_SENTINEL", True)
    else:
        actor = User.objects.get(username="publisher")
        app = Application.objects.get(slug="builder-fixture")
    client = Client()
    client.force_login(actor)

    def queue():
        response = client.post(
            reverse("upload", args=[app.pk]),
            {
                "archive": SimpleUploadedFile("app.zip", (args.state / "fixture.zip").read_bytes()),
                "idempotency_key": str(uuid.uuid4()),
            },
        )
        assert response.status_code == 302
        upload = Job.objects.get(pk=response["Location"].strip("/").split("/")[-1])
        process_one()
        upload.refresh_from_db()
        assert upload.version.status == "validated"
        form = client.get(reverse("version_prepare", args=[upload.version_id]))
        assert form.status_code == 200 and b"r-a" in form.content
        response = client.post(
            reverse("version_prepare", args=[upload.version_id]),
            {
                "environment": "r-a",
                "idempotency_key": str(uuid.uuid4()),
            },
        )
        assert response.status_code == 302
        return Job.objects.get(pk=response["Location"].strip("/").split("/")[-1])

    def await_terminal(job):
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            process_one()
            job.refresh_from_db()
            if job.status not in {"queued", "running"}:
                return
            time.sleep(1)
        raise RuntimeError("Panel job timed out.")

    if args.stage == "submit":
        job = queue()
        assert process_one()
        job.refresh_from_db()
        assert job.status == "running"
        print(json.dumps({"job_id": str(job.pk), "app_id": str(app.pk), "passed": True}))
    else:
        job = Job.objects.get(app=app, kind="prepare_version")
        await_terminal(job)
        assert job.status == "succeeded", (job.phase, job.message)
        assert job.version.status == "prepared"
        assert not job.version.build_manifest["cache_hit"]
        page = client.get(reverse("job_detail", args=[job.pk]))
        assert page.status_code == 200 and b"Imagem preparada" in page.content
        assert b"PRIVATE_PANEL_SENTINEL" not in page.content
        assert "PRIVATE_PANEL_SENTINEL" not in json.dumps(job.version.build_manifest)
        app.refresh_from_db()
        assert app.active_publication_id is None
        cancelled = queue()
        assert client.post(reverse("job_cancel", args=[cancelled.pk])).status_code == 302
        await_terminal(cancelled)
        assert cancelled.status == "cancelled" and not cancelled.version.image_digest
        outsider = User.objects.create_user("outsider")
        client.force_login(outsider)
        assert client.get(reverse("version_prepare", args=[job.version_id])).status_code == 404
        assert client.get(reverse("job_detail", args=[job.pk])).status_code == 404
        print(
            json.dumps(
                {
                    "passed": True,
                    "job_ids": [str(job.pk), str(cancelled.pk)],
                    "artifact": job.version.build_manifest,
                    "checks": [
                        "panel_upload_prepare",
                        "worker_reconciles_after_api_restart",
                        "fresh_artifact",
                        "panel_cancel_confirmed",
                        "panel_object_permissions",
                        "no_secret_in_build_or_page",
                    ],
                }
            )
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Only fixture locations/types, never values, locals or credentials.
        print(
            json.dumps(
                {
                    "passed": False,
                    "exception": type(exc).__name__,
                    "frames": [
                        {"file": Path(frame.filename).name, "line": frame.lineno}
                        for frame in traceback.extract_tb(exc.__traceback__)
                    ],
                }
            )
        )
        raise SystemExit(1) from None

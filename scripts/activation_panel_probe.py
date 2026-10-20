#!/usr/bin/env python3
"""Exercise activation as shinyd-control using only the disposable panel fixture."""

import argparse
import io
import json
import os
import sys
import time
import traceback
import uuid
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, type=Path)
    args = parser.parse_args()
    if os.geteuid() == 0:
        raise RuntimeError("Use the control identity.")
    os.environ.update(
        {
            "SHINYD_DEV": "1",
            "SHINYD_STATE_DIR": str(args.state),
            "SHINYD_ALLOWED_HOSTS": "panel.localhost",
            "SHINYD_RUNTIME_ENABLED": "1",
            "SHINYD_PANEL_ORIGIN": "http://panel.localhost",
            "SHINYD_APP_DOMAIN": "apps.localhost",
            "DJANGO_SETTINGS_MODULE": "shinyd_web.settings",
        }
    )
    sys.path.insert(0, str(ROOT / "backend"))
    import django

    django.setup()
    from django.core.files.uploadedfile import SimpleUploadedFile
    from django.test import Client
    from django.urls import reverse

    from control import runtime_client
    from control.gateway_auth import authorize, exchange, issue_ticket
    from control.jobs import process_one
    from control.models import AppInstance, Application, Job, User, Version

    app = Application.objects.get(slug="builder-fixture")
    actor = User.objects.get(username="publisher")
    client = Client(HTTP_HOST="panel.localhost")
    client.force_login(actor)
    instances, checks, busy_retries = [], [], []

    def check(name, value):
        assert value, name
        checks.append(name)

    def rejected(name, expected, operation):
        for attempt in range(4):
            try:
                operation()
            except runtime_client.RuntimeRejected as exc:
                if exc.code == "runtime_busy" and attempt < 3:
                    busy_retries.append(name)
                    time.sleep(5)
                    continue
                check(name, exc.code == expected)
                return
            raise AssertionError(name)

    def wait(job):
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            process_one()
            job.refresh_from_db()
            if job.status not in {"running", "queued"}:
                return job
            time.sleep(1)
        raise RuntimeError("Fixture deadline exceeded.")

    def activate(version):
        response = client.post(
            reverse("version_activate", args=[version.pk]),
            {
                "idempotency_key": str(uuid.uuid4()),
                "acknowledge": True,
            },
        )
        assert response.status_code == 302
        job = Job.objects.get(pk=response["Location"].strip("/").split("/")[-1])
        instances.append(str(job.publication_id))
        (args.state / "activation-instances.json").write_text(json.dumps(instances))
        return job

    version = Version.objects.get(app=app, status="prepared")
    first = wait(activate(version))
    check("panel_activation_ready", first.status == "succeeded")
    check("activation_with_encrypted_environment", first.publication.config_revision > 0)
    app.refresh_from_db()
    instance = AppInstance.objects.get(pk=first.publication_id)
    check("activation_private_route_denied", not authorize(instance.host, "")["allowed"])
    code = issue_ticket(actor, client.session.session_key, instance)
    credential = exchange(instance.host, code)["credential"]
    check(
        "activation_authenticated_route",
        authorize(instance.host, credential)["target"] == instance.target,
    )
    app.visibility = "public"
    app.save(update_fields=["visibility"])
    check("activation_public_route", authorize(instance.host, "")["allowed"])
    app.visibility = "private"
    app.save(update_fields=["visibility"])
    client.force_login(User.objects.get(username="outsider"))
    check(
        "activation_other_user_denied",
        client.get(reverse("version_activate", args=[version.pk])).status_code == 404,
    )
    client.force_login(actor)
    foreign = {**first.publication.runtime_spec, "app_id": str(uuid.uuid4())}
    foreign_id = uuid.uuid4()
    rejected(
        "activation_cross_app_artifact_denied",
        "artifact_rejected",
        lambda: runtime_client.request("PUT", foreign_id, foreign, environment={}),
    )

    def prepare(data):
        response = client.post(
            reverse("upload", args=[app.pk]),
            {
                "archive": SimpleUploadedFile("app.zip", data),
                "idempotency_key": str(uuid.uuid4()),
            },
        )
        assert response.status_code == 302
        upload = wait(Job.objects.get(pk=response["Location"].strip("/").split("/")[-1]))
        assert upload.status == "succeeded"
        response = client.post(
            reverse("version_prepare", args=[upload.version_id]),
            {
                "environment": "r-a",
                "idempotency_key": str(uuid.uuid4()),
            },
        )
        assert response.status_code == 302
        build = wait(Job.objects.get(pk=response["Location"].strip("/").split("/")[-1]))
        assert build.status == "succeeded"
        return build.version

    bad = io.BytesIO()
    with (
        zipfile.ZipFile(args.state / "fixture.zip") as original,
        zipfile.ZipFile(bad, "w") as archive,
    ):
        archive.writestr("project/renv.lock", original.read("project/renv.lock"))
        archive.writestr("project/app.R", "stop('PRIVATE_STARTUP_SENTINEL')")
    failed = wait(activate(prepare(bad.getvalue())))
    app.refresh_from_db()
    check(
        "activation_failed_start_preserves_previous",
        failed.status == "failed"
        and app.active_publication_id == first.publication_id
        and runtime_client.inspect(first.publication)["state"] == "ready"
        and AppInstance.objects.get(pk=first.publication_id).enabled,
    )
    check("activation_failure_has_safe_message", "PRIVATE_STARTUP_SENTINEL" not in failed.message)

    new_version = prepare((args.state / "fixture.zip").read_bytes())
    cancelled = activate(new_version)
    client.post(reverse("job_cancel", args=[cancelled.pk]))
    wait(cancelled)
    check("activation_cancel_confirmed", cancelled.status == "cancelled")
    rejected(
        "activation_late_launch_denied",
        "instance_conflict",
        lambda: runtime_client.ensure(cancelled.publication),
    )
    replacement = wait(activate(new_version))
    app.refresh_from_db()
    instance.refresh_from_db()
    check(
        "activation_switch_and_retirement",
        replacement.status == "succeeded"
        and app.active_publication_id == replacement.publication_id
        and not instance.enabled
        and runtime_client.inspect(first.publication)["state"] == "removed",
    )
    check("activation_old_host_revoked", not authorize(instance.host, credential)["allowed"])
    print(
        json.dumps(
            {
                "passed": True,
                "checks": checks,
                "instances": instances,
                "active_instance": str(replacement.publication_id),
                "busy_retries": busy_retries,
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
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

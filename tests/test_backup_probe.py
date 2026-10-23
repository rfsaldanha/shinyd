import hashlib
import io
from contextlib import redirect_stdout

import pytest
from backup_browser_probe import prepare_environment
from probe_progress import progress

from control.environment import snapshot
from control.models import EnvironmentValue, Publication, Version
from control.services import decrypt_environment, remove_environment, set_environment


@pytest.mark.parametrize("removed", [True, False])
def test_backup_prepares_own_secret_even_after_previous_removal(
    app, accounts, local_state, monkeypatch, removed
):
    old_token, new_token = "earlier-synthetic-token", "new-synthetic-backup-token"
    message = "synthetic message\nç='quoted'\\value\r\nnext line"
    version = Version.objects.create(
        app=app,
        created_by=accounts["publisher"],
        status="prepared",
        image_digest="sha256:" + "a" * 64,
    )
    app.last_version = version
    app.save()
    set_environment(app.owner, app.pk, 0, "APP_TOKEN", old_token, True)
    set_environment(app.owner, app.pk, 1, "APP_MESSAGE", message, True)
    publication = Publication.objects.create(
        app=app,
        version=version,
        config_revision=2,
        created_by=app.owner,
        status="retired",
    )
    publication.environment_snapshot = snapshot(publication)
    publication.save()
    historical = bytes(publication.environment_snapshot)
    if removed:
        remove_environment(app.owner, app.pk, 2, "APP_TOKEN")
        # This is the read that aborted the installed probe before the fix.
        with pytest.raises(EnvironmentValue.DoesNotExist):
            app.environment.get(name="APP_TOKEN")
    app.refresh_from_db()
    revision = app.config_revision
    captured = []

    def admin(code):
        # Execute the exact program delivered over shinyd-admin's stdin, using
        # real ORM records and encryption, not a canned command result.
        output = io.StringIO()
        with redirect_stdout(output):
            exec(code, {})
        captured.append(output.getvalue())
        return output.getvalue()

    monkeypatch.setattr("backup_browser_probe.secrets.token_urlsafe", lambda n: new_token)
    expected = prepare_environment(admin, str(app.pk))
    assert expected == {
        "image": version.image_digest,
        "fingerprint": "|".join(
            hashlib.sha256(v.encode()).hexdigest() for v in (new_token, message)
        ),
    }
    app.refresh_from_db()
    publication.refresh_from_db()
    assert app.config_revision == revision + 1
    assert app.desired_state == "stopped" and app.active_publication_id is None
    assert app.last_version_id == version.pk
    assert bytes(publication.environment_snapshot) == historical
    assert decrypt_environment(app.environment.get(name="APP_TOKEN")) == new_token
    assert decrypt_environment(app.environment.get(name="APP_MESSAGE")) == message
    assert all(v not in "".join(captured) for v in (old_token, new_token, message))
    assert new_token.encode() not in bytes(app.environment.get(name="APP_TOKEN").ciphertext)


def test_fixture_setup_refuses_a_running_app(app, local_state):
    app.desired_state = "running"
    app.save()
    with pytest.raises(AssertionError):
        prepare_environment(lambda code: exec(code, {}), str(app.pk))
    assert not app.environment.exists()


def test_progress_preserves_json_report_stdout(capsys):
    progress("backup_create")
    output = capsys.readouterr()
    assert output.out == ""
    assert "Criando o backup criptografado" in output.err

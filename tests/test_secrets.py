import json

import pytest
from cryptography.exceptions import InvalidTag
from django.core.exceptions import ValidationError
from django.urls import reverse

from control.models import AuditEvent, EnvironmentValue
from control.services import Conflict, decrypt_environment, set_environment

pytestmark = pytest.mark.django_db


def test_secret_encrypted_no_render_or_audit(client, app, accounts, local_state):
    secret = "my-actual-super-sensitive-token"
    set_environment(accounts["publisher"], app.pk, 0, "API_TOKEN", secret, True)
    value = EnvironmentValue.objects.get(app=app)
    assert secret.encode() not in bytes(value.ciphertext)
    assert decrypt_environment(value) == secret
    assert secret not in json.dumps(list(AuditEvent.objects.values("details")))
    client.force_login(accounts["publisher"])
    page = client.get(reverse("app_detail", args=[app.pk]))
    assert secret.encode() not in page.content
    assert b"API_TOKEN" in page.content
    assert secret.encode() not in client.get("/api/v1/apps/").content


def test_configuration_revision_and_reserved_names(app, accounts, local_state):
    set_environment(accounts["publisher"], app.pk, 0, "DB_PASSWORD", "one", True)
    with pytest.raises(Conflict):
        set_environment(accounts["publisher"], app.pk, 0, "DB_PASSWORD", "two", True)
    with pytest.raises(ValidationError):
        set_environment(accounts["publisher"], app.pk, 1, "LD_PRELOAD", "evil", True)
    assert decrypt_environment(EnvironmentValue.objects.get(app=app)) == "one"


def test_ciphertext_bound_to_app_name_and_revision(app, accounts, local_state):
    set_environment(accounts["publisher"], app.pk, 0, "TOKEN", "one", True)
    value = EnvironmentValue.objects.get(app=app)
    value.name = "OTHER_TOKEN"
    with pytest.raises(InvalidTag):
        decrypt_environment(value)


def test_insecure_key_permissions_do_not_write_config(app, accounts, local_state, settings):
    settings.SHINYD_MASTER_KEY_FILE.chmod(0o644)
    with pytest.raises(ValidationError):
        set_environment(accounts["publisher"], app.pk, 0, "TOKEN", "one", True)
    assert not EnvironmentValue.objects.exists()
    app.refresh_from_db()
    assert app.config_revision == 0

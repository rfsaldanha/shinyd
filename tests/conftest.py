import os

import pytest
from django.contrib.auth import get_user_model

from control.models import Application, PriorityGroup


@pytest.fixture
def local_state(settings, tmp_path):
    settings.STATE_DIR = tmp_path
    key = tmp_path / "master.key"
    key.write_bytes(os.urandom(32))
    key.chmod(0o600)
    settings.SHINYD_MASTER_KEY_FILE = key
    return tmp_path


@pytest.fixture
def accounts(db):
    user = get_user_model()
    return {
        "admin": user.objects.create_superuser("admin", password="strong-test-pass-123"),
        "publisher": user.objects.create_user(
            "publisher", password="strong-test-pass-123", can_publish=True
        ),
        "viewer": user.objects.create_user("viewer", password="strong-test-pass-123"),
        "outsider": user.objects.create_user("outsider", password="strong-test-pass-123"),
    }


@pytest.fixture
def app(accounts):
    normal, _ = PriorityGroup.objects.get_or_create(
        slug="normal", defaults={"name": "Normal", "weight": 200}
    )
    return Application.objects.create(
        name="Meu app", slug="meu-app", owner=accounts["publisher"], priority=normal
    )

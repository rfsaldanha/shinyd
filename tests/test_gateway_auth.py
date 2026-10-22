import io
import json
import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from control.gateway_api import GatewayAPI, load_key
from control.gateway_auth import (
    TooManyConnections,
    authorize,
    digest,
    exchange,
    instance_origin,
    issue_ticket,
    route,
)
from control.models import AppGrant, AppInstance, AuditEvent, GatewaySession, GatewayTicket

pytestmark = pytest.mark.django_db
SERVICE_KEY = "ab" * 32


def test_unix_runtime_route_is_bound_to_instance(registration):
    registration.target = f"unix:/run/shinyd-runtime/apps/{registration.pk}.sock"
    assert route(registration)["target"] == registration.target
    for target in (
        "unix:/run/podman/podman.sock",
        f"unix:/tmp/{registration.pk}.sock",
        registration.target + "?x=1",
        "unix:/run/shinyd-runtime/apps/11111111-1111-4111-8111-111111111111.sock",
    ):
        registration.target = target
        with pytest.raises(ValidationError):
            route(registration)


@pytest.fixture
def registration(settings, app, accounts, client):
    settings.DEBUG = True
    settings.SHINYD_PANEL_ORIGIN = "http://panel.localhost:8000"
    settings.ALLOWED_HOSTS = ["panel.localhost", "testserver"]
    AppGrant.objects.create(app=app, user=accounts["viewer"], role="viewer")
    client.force_login(accounts["viewer"])
    return AppInstance.objects.create(
        app=app, host="app.localhost:9090", target="http://127.0.0.1:3838", enabled=True
    )


def ticket(client, user, registration):
    return issue_ticket(user, client.session.session_key, registration)


def credential(client, user, registration):
    return exchange(registration.host, ticket(client, user, registration))["credential"]


def test_handoff_is_host_bound_single_use_and_hash_only(client, registration, accounts):
    code = ticket(client, accounts["viewer"], registration)
    with pytest.raises(PermissionDenied):
        exchange("other.localhost:9090", code)
    result = exchange(registration.host, code)
    with pytest.raises(PermissionDenied):
        exchange(registration.host, code)
    assert GatewayTicket.objects.get().token_hash == digest(code)
    assert GatewaySession.objects.get().token_hash == digest(result["credential"])
    assert 1 <= result["max_age"] <= 3600
    all_records = str(list(GatewayTicket.objects.values()) + list(GatewaySession.objects.values()))
    all_records += str(list(AuditEvent.objects.values()))
    assert code not in all_records
    assert result["credential"] not in all_records
    assert authorize(registration.host, result["credential"])["allowed"]
    other = AppInstance.objects.create(
        app=registration.app, host="other.localhost:9090", target=registration.target, enabled=True
    )
    assert not authorize(other.host, result["credential"])["allowed"]


@pytest.mark.parametrize("change", ["logout", "password", "disabled", "revoke", "expiry", "stop"])
def test_revocation_is_seen_on_next_check(client, registration, accounts, change):
    user = accounts["viewer"]
    token = credential(client, user, registration)
    assert authorize(registration.host, token)["allowed"]
    if change == "logout":
        client.logout()
    elif change == "password":
        user.set_password("new-password-that-invalidates-the-old-session")
        user.save()
    elif change == "disabled":
        user.is_active = False
        user.save()
    elif change == "revoke":
        AppGrant.objects.filter(app=registration.app, user=user).delete()
    elif change == "expiry":
        GatewaySession.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    else:
        registration.enabled = False
        registration.save()
    assert not authorize(registration.host, token)["allowed"]


def test_expired_ticket_and_changed_grants_cannot_be_exchanged(client, registration, accounts):
    code = ticket(client, accounts["viewer"], registration)
    GatewayTicket.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    with pytest.raises(PermissionDenied):
        exchange(registration.host, code)
    code = ticket(client, accounts["viewer"], registration)
    AppGrant.objects.filter(app=registration.app, user=accounts["viewer"]).delete()
    with pytest.raises(PermissionDenied):
        exchange(registration.host, code)


def test_public_visibility_is_dynamic(registration):
    assert not authorize(registration.host, "")["allowed"]
    registration.app.visibility = "public"
    registration.app.save()
    assert authorize(registration.host, "")["allowed"]
    registration.app.visibility = "private"
    registration.app.save()
    assert not authorize(registration.host, "")["allowed"]


def test_private_ticket_cannot_be_issued_by_an_outsider(client, registration, accounts):
    client.force_login(accounts["outsider"])
    with pytest.raises(PermissionDenied):
        ticket(client, accounts["outsider"], registration)
    client.force_login(accounts["viewer"])
    with pytest.raises(PermissionDenied):
        issue_ticket(accounts["viewer"], "nonexistent-session", registration)


def test_connect_requires_csrf_and_does_not_put_code_in_url(registration, accounts):
    client = Client(enforce_csrf_checks=True)
    client.force_login(accounts["viewer"])
    path = reverse("gateway_connect", args=[registration.pk])
    headers = {"HTTP_HOST": "panel.localhost:8000"}
    assert client.get(path, **headers).status_code == 200
    assert client.post(path, **headers).status_code == 403
    assert not GatewayTicket.objects.exists()
    csrf = client.cookies["shinyd_dev_csrf"].value
    response = client.post(path, {"csrfmiddlewaretoken": csrf}, **headers)
    assert response.status_code == 200
    code = response.context["code"]
    assert code not in str(response.headers)
    assert response["Cache-Control"] == "no-store"
    assert response["Referrer-Policy"] == "strict-origin"
    assert "form-action http://app.localhost:9090" in response["Content-Security-Policy"]
    assert response.context["action"] == "http://app.localhost:9090/_shinyd/connect"
    assert client.get(path, HTTP_HOST="testserver").status_code == 403


def test_handoff_backlog_and_sessions_are_bounded(client, registration, accounts):
    for _ in range(5):
        ticket(client, accounts["viewer"], registration)
    with pytest.raises(TooManyConnections):
        ticket(client, accounts["viewer"], registration)
    GatewayTicket.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    first = GatewayTicket.objects.first()
    for index in range(32):
        GatewaySession.objects.create(
            token_hash=f"{index:064x}",
            instance=registration,
            user=accounts["viewer"],
            panel_session=first.panel_session,
            expires_at=timezone.now() + timedelta(hours=1),
        )
    with pytest.raises(TooManyConnections):
        ticket(client, accounts["viewer"], registration)


@pytest.mark.parametrize(
    "target",
    [
        "http://169.254.169.254:80",
        "http://example.com:80",
        "http://127.0.0.1:80/path",
        "http://user@127.0.0.1:80",
        "http://127.0.0.1",
        "file:///etc/passwd",
    ],
)
def test_unsafe_targets_are_refused(registration, target):
    registration.target = target
    with pytest.raises(ValidationError):
        route(registration)


def test_production_requires_https_app_domain_and_separate_panel_host(settings, registration):
    settings.DEBUG = False
    settings.SHINYD_PANEL_ORIGIN = "https://panel.example.org"
    settings.SHINYD_APP_DOMAIN = "apps.example.org"
    with pytest.raises(ValidationError):
        instance_origin(registration)
    registration.host = "instance.apps.example.org"
    assert instance_origin(registration) == "https://instance.apps.example.org"
    registration.host = "panel.example.org:9090"
    with pytest.raises(ValidationError):
        instance_origin(registration)
    settings.SHINYD_PANEL_ORIGIN = "http://panel.example.org"
    with pytest.raises(ValidationError):
        instance_origin(registration)


def call_api(path, body, key=SERVICE_KEY, **extra):
    raw = json.dumps(body).encode()
    env = {
        "REQUEST_METHOD": "POST",
        "PATH_INFO": path,
        "QUERY_STRING": "",
        "HTTP_AUTHORIZATION": "Bearer " + key,
        "CONTENT_TYPE": "application/json",
        "CONTENT_LENGTH": str(len(raw)),
        "wsgi.input": io.BytesIO(raw),
        **extra,
    }
    status = []
    # pytest holds a transaction across the test; the real service has no such wrapper.
    with patch("control.gateway_api.close_old_connections"):
        data = b"".join(GatewayAPI(SERVICE_KEY)(env, lambda code, headers: status.append(code)))
    return int(status[0].split()[0]), json.loads(data)


def test_internal_api_is_separate_authenticated_and_strict(client, registration):
    body = {
        "operation": "open",
        "request_id": str(uuid.uuid4()),
        "kind": "http",
        "host": registration.host,
        "credential": "",
    }
    assert client.post("/v2/request", body).status_code == 404
    assert call_api("/v1/authorize", body)[0] == 404  # No untracked legacy forwarding.
    assert call_api("/v2/request", body, key="wrong")[0] == 403
    assert call_api("/v2/request", body, REQUEST_METHOD="GET")[0] == 405
    assert call_api("/v2/request", {**body, "target": "injection"})[0] == 400
    assert call_api("/v2/request", body, CONTENT_LENGTH="2049")[0] == 400
    assert call_api("/v2/request", body, QUERY_STRING="credential=forbidden")[0] == 404
    assert call_api("/v2/request", body) == (
        200,
        {"allowed": False, "instance_id": str(registration.pk)},
    )


def test_internal_api_fails_closed_without_logging_credentials(registration, caplog):
    sensitive = "must-never-be-logged"
    with patch("control.gateway_api.track", side_effect=RuntimeError(sensitive)):
        status, result = call_api(
            "/v2/request",
            {
                "operation": "open",
                "request_id": str(uuid.uuid4()),
                "kind": "http",
                "host": registration.host,
                "credential": sensitive,
            },
        )
    assert status == 503 and not result["allowed"]
    assert sensitive not in caplog.text
    assert SERVICE_KEY not in caplog.text


def test_internal_key_permissions_are_enforced(settings, tmp_path):
    key = tmp_path / "gateway.key"
    key.write_text(SERVICE_KEY)
    settings.SHINYD_GATEWAY_KEY_FILE = key
    key.chmod(0o600)
    assert load_key() == SERVICE_KEY
    key.chmod(0o644)
    with pytest.raises(ValueError):
        load_key()


def test_cleanup_only_removes_expired_credentials(client, registration, accounts):
    token = credential(client, accounts["viewer"], registration)
    GatewayTicket.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    call_command("cleargateway", stdout=io.StringIO())
    assert not GatewayTicket.objects.exists()
    assert authorize(registration.host, token)["allowed"]

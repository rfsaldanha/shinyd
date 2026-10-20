import pytest
from django.contrib.auth.models import Group
from django.test import Client
from django.urls import reverse

from control.models import AppGrant, AuditEvent, User
from control.permissions import manageable_apps, visible_apps
from control.services import Conflict, create_app, update_app

pytestmark = pytest.mark.django_db


def test_private_app_not_visible_to_outsider_or_anonymous(client, app, accounts):
    url = reverse("app_detail", args=[app.pk])
    assert client.get(url).status_code == 302
    client.force_login(accounts["outsider"])
    assert client.get(url).status_code == 404
    assert client.get("/api/v1/apps/").json()["apps"] == []
    assert client.post(reverse("app_config", args=[app.pk]), {}).status_code == 404


def test_viewer_cannot_publish_or_administer(client, app, accounts):
    AppGrant.objects.create(app=app, user=accounts["viewer"], role="viewer")
    client.force_login(accounts["viewer"])
    assert client.get(reverse("app_detail", args=[app.pk])).status_code == 200
    for name in ["app_edit", "upload", "app_access", "app_config"]:
        assert client.get(reverse(name, args=[app.pk])).status_code == 404
    assert client.get("/users/").status_code == 403
    assert client.get("/apps/new/").status_code == 403


def test_group_publisher_and_inactive_user(app, accounts):
    group = Group.objects.create(name="Equipe")
    accounts["viewer"].groups.add(group)
    AppGrant.objects.create(app=app, group=group, role="publisher")
    assert manageable_apps(accounts["viewer"]).filter(pk=app.pk).exists()
    accounts["viewer"].is_active = False
    assert not manageable_apps(accounts["viewer"]).exists()
    assert not visible_apps(accounts["viewer"]).filter(pk=app.pk).exists()


def test_public_app_not_public_management(client, app, accounts):
    app.visibility = "public"
    app.save()
    client.force_login(accounts["outsider"])
    assert client.get(reverse("app_detail", args=[app.pk])).status_code == 200
    assert client.get(reverse("upload", args=[app.pk])).status_code == 404


def test_csrf_and_logout_require_post(app, accounts):
    client = Client(enforce_csrf_checks=True)
    client.force_login(accounts["publisher"])
    assert client.post(reverse("app_edit", args=[app.pk]), {}).status_code == 403
    assert client.get("/logout/").status_code == 405


def test_revision_conflict_preserves_concurrent_edit(app, accounts):
    update_app(accounts["publisher"], app.pk, 1, {"name": "Novo"})
    with pytest.raises(Conflict):
        update_app(accounts["publisher"], app.pk, 1, {"name": "Perdido"})
    app.refresh_from_db()
    assert app.name == "Novo"
    assert app.revision == 2


def test_admin_pages_render(client, app, accounts):
    AppGrant.objects.create(app=app, user=accounts["viewer"], role="viewer")
    client.force_login(accounts["admin"])
    for url in [
        "/",
        "/users/",
        "/audit/",
        "/users/new/",
        "/password/",
        reverse("app_detail", args=[app.pk]),
        reverse("app_edit", args=[app.pk]),
    ]:
        response = client.get(url)
        assert response.status_code == 200, url
        assert response["Content-Security-Policy"].find("frame-ancestors 'none'") >= 0
        assert response["Cache-Control"] == "no-store"


def test_no_superuser_escalation_from_account_form(client, accounts):
    client.force_login(accounts["admin"])
    response = client.post(
        reverse("user_edit", args=[accounts["viewer"].pk]),
        {
            "email": "",
            "first_name": "",
            "last_name": "",
            "is_active": "on",
            "is_superuser": "on",
        },
    )
    assert response.status_code == 302
    accounts["viewer"].refresh_from_db()
    assert not accounts["viewer"].is_superuser


def test_last_admin_cannot_be_disabled(client, accounts):
    client.force_login(accounts["admin"])
    response = client.post(
        reverse("user_edit", args=[accounts["admin"].pk]),
        {
            "email": "",
            "first_name": "",
            "last_name": "",
        },
    )
    assert response.status_code == 200
    accounts["admin"].refresh_from_db()
    assert accounts["admin"].is_active


def test_login_throttles_without_revealing_account(client, accounts):
    for _ in range(5):
        assert client.post("/login/", {"username": "admin", "password": "wrong"}).status_code == 200
    response = client.post("/login/", {"username": "admin", "password": "wrong"})
    assert response.status_code == 429
    assert response["Retry-After"] == "900"


def test_recovery_token_not_audited_and_single_use(client, accounts):
    client.force_login(accounts["admin"])
    response = client.post(reverse("recovery_issue", args=[accounts["viewer"].pk]))
    path = response.context["recovery_path"]
    assert path not in str(list(AuditEvent.objects.values()))
    client.logout()
    response = client.get(path)
    assert response.status_code == 302
    reset_path = response["Location"]
    response = client.post(
        reset_path,
        {
            "new_password1": "another-long-test-password",
            "new_password2": "another-long-test-password",
        },
    )
    assert response.status_code == 302
    assert User.objects.get(pk=accounts["viewer"].pk).check_password("another-long-test-password")
    response = client.get(path)
    assert not response.context["validlink"]


def test_creation_authorization_enforced_at_service(app, accounts):
    from django.core.exceptions import PermissionDenied

    with pytest.raises(PermissionDenied):
        create_app(
            accounts["viewer"], {"name": "Forbidden", "slug": "forbidden", "priority": app.priority}
        )


def test_disabled_then_reenabled_user_does_not_regain_old_session(client, accounts):
    viewer = Client()
    viewer.force_login(accounts["viewer"])
    client.force_login(accounts["admin"])
    url = reverse("user_edit", args=[accounts["viewer"].pk])
    assert client.post(url, {"email": "", "first_name": "", "last_name": ""}).status_code == 302
    assert (
        client.post(
            url,
            {
                "email": "",
                "first_name": "",
                "last_name": "",
                "is_active": "on",
            },
        ).status_code
        == 302
    )
    assert viewer.get("/").status_code == 302

"""Central authorization. Tokens never appear in routes, logs or audit details."""

import hashlib
import ipaddress
import re
import secrets
from datetime import timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.sessions.models import Session
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare

from .models import AppInstance, Application, GatewaySession, GatewayTicket
from .permissions import visible_apps
from .services import event

TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")
HOST = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)*(?::[0-9]{1,5})?$")


class TooManyConnections(Exception):
    pass


def digest(token):
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
        raise PermissionDenied
    return hashlib.sha256(token.encode()).hexdigest()


def instance_origin(instance):
    panel = urlsplit(settings.SHINYD_PANEL_ORIGIN)
    scheme = "http" if settings.DEBUG else "https"
    if (
        panel.scheme != scheme
        or not panel.hostname
        or panel.username is not None
        or panel.password is not None
        or panel.path
        or panel.query
        or panel.fragment
        or not HOST.fullmatch(panel.netloc)
        or not HOST.fullmatch(instance.host)
    ):
        raise ValidationError("Origem do painel ou da instância inválida.")
    host = urlsplit("//" + instance.host)
    if host.hostname == panel.hostname:
        raise ValidationError("Painel e apps exigem nomes de host distintos.")
    for address in (panel, host):
        if address.port is not None and not 1 <= address.port <= 65535:
            raise ValidationError("Porta inválida.")
    if settings.DEBUG:
        if any(
            value != "localhost" and not value.endswith(".localhost")
            for value in (panel.hostname, host.hostname)
        ):
            raise ValidationError("HTTP de desenvolvimento exige hosts localhost.")
    else:
        domain = settings.SHINYD_APP_DOMAIN
        if not domain or not HOST.fullmatch(domain) or ":" in domain:
            raise ValidationError("Domínio dos apps não configurado.")
        if not host.hostname.endswith("." + domain):
            raise ValidationError("Instância fora do domínio dos apps.")
    return scheme + "://" + instance.host


def route(instance):
    instance_origin(instance)
    # Exact instance-bound path, supplied only by the trusted runtime. No
    # arbitrary socket, URI encoding, path traversal or cross-instance alias.
    expected_socket = f"unix:/run/shinyd-runtime/apps/{instance.pk}.sock"
    if instance.target == expected_socket:
        return {"instance_id": str(instance.pk), "target": instance.target}
    target = urlsplit(instance.target)
    try:
        valid = (
            target.scheme == "http"
            and target.username is None
            and target.password is None
            and ipaddress.ip_address(target.hostname).is_loopback
            and target.port is not None
            and 1 <= target.port <= 65535
            and target.path in ("", "/")
            and not target.query
            and not target.fragment
        )
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ValidationError("Destino interno inválido.")
    return {"instance_id": str(instance.pk), "target": instance.target}


def session_valid(user, session):
    if not user.is_active or session.expire_date <= timezone.now():
        return False
    data = session.get_decoded()
    return (
        str(data.get("_auth_user_id")) == str(user.pk)
        and data.get("_auth_user_backend") in settings.AUTHENTICATION_BACKENDS
        and constant_time_compare(data.get("_auth_user_hash", ""), user.get_session_auth_hash())
    )


def authorized_subject(item):
    return (
        session_valid(item.user, item.panel_session)
        and visible_apps(item.user).filter(pk=item.instance.app_id).exists()
    )


@transaction.atomic
def issue_ticket(user, session_key, instance):
    if not instance.enabled or not visible_apps(user).filter(pk=instance.app_id).exists():
        raise PermissionDenied
    route(instance)
    session = Session.objects.filter(session_key=session_key).first()
    if not session or not session_valid(user, session):
        raise PermissionDenied
    now = timezone.now()
    # Bound abandoned handoffs without retaining raw credentials.
    GatewayTicket.objects.filter(panel_session=session, expires_at__lte=now).delete()
    if (
        GatewayTicket.objects.filter(
            panel_session=session, consumed_at__isnull=True, expires_at__gt=now
        ).count()
        >= 5
    ):
        raise TooManyConnections
    if (
        GatewaySession.objects.filter(
            panel_session=session, instance=instance, expires_at__gt=now
        ).count()
        >= 32
    ):
        raise TooManyConnections
    token = secrets.token_urlsafe(32)
    GatewayTicket.objects.create(
        token_hash=digest(token),
        instance=instance,
        user=user,
        panel_session=session,
        expires_at=min(now + timedelta(seconds=60), session.expire_date),
    )
    event(user, "gateway.ticket_issued", instance.pk)
    return token


@transaction.atomic
def exchange(host, code):
    now = timezone.now()
    ticket = (
        GatewayTicket.objects.select_related("instance", "user", "panel_session")
        .filter(
            token_hash=digest(code),
            instance__host=host,
            instance__enabled=True,
            consumed_at__isnull=True,
            expires_at__gt=now,
        )
        .first()
    )
    if not ticket or not authorized_subject(ticket):
        raise PermissionDenied
    route(ticket.instance)
    if (
        GatewaySession.objects.filter(
            panel_session=ticket.panel_session, instance=ticket.instance, expires_at__gt=now
        ).count()
        >= 32
    ):
        raise TooManyConnections
    # Compare-and-set prevents replay even on a database without row locks.
    if not GatewayTicket.objects.filter(
        pk=ticket.pk, consumed_at__isnull=True, expires_at__gt=now
    ).update(consumed_at=now):
        raise PermissionDenied
    token = secrets.token_urlsafe(32)
    expiry = min(now + timedelta(hours=1), ticket.panel_session.expire_date)
    GatewaySession.objects.create(
        token_hash=digest(token),
        instance=ticket.instance,
        user=ticket.user,
        panel_session=ticket.panel_session,
        expires_at=expiry,
    )
    event(ticket.user, "gateway.session_started", ticket.instance_id)
    return {"credential": token, "max_age": max(1, int((expiry - now).total_seconds()))}


def authorize(host, credential):
    instance = AppInstance.objects.select_related("app").filter(host=host, enabled=True).first()
    if not instance:
        return {"allowed": False, "reason": "not_found"}
    data = route(instance)
    if instance.app.visibility == Application.Visibility.PUBLIC:
        return {"allowed": True, **data}
    if not credential:
        return {"allowed": False, "instance_id": str(instance.pk)}
    session = (
        GatewaySession.objects.select_related("instance", "user", "panel_session")
        .filter(token_hash=digest(credential), instance=instance, expires_at__gt=timezone.now())
        .first()
    )
    if not session or not authorized_subject(session):
        return {"allowed": False, "instance_id": str(instance.pk)}
    return {"allowed": True, **data}

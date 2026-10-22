"""Atomic admission and conservative telemetry for the managed gateway.

Every proxied request is reserved before forwarding. Closing a reservation is
explicit: a missed heartbeat must never authorize retirement of an R process.
Transactions share the same SQLite IMMEDIATE lock as publication cutover.
"""

import uuid
from datetime import timedelta

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from .gateway_auth import TooManyConnections, authorize
from .models import AppInstance, GatewayRequest

STALE_AFTER = timedelta(seconds=60)


def counts(instance_id):
    return GatewayRequest.objects.filter(instance_id=instance_id).aggregate(
        total=Count("id"),
        websockets=Count("id", filter=Q(kind="websocket", upgraded=True)),
        opening=Count("id", filter=Q(kind="websocket", upgraded=False)),
        http=Count("id", filter=Q(kind="http")),
        uncertain=Count("id", filter=Q(heartbeat_at__lt=timezone.now() - STALE_AFTER)),
    )


@transaction.atomic
def track(*, operation, request_id, host, credential, kind):
    identifier = uuid.UUID(request_id)
    if str(identifier) != request_id or kind not in {"http", "websocket"}:
        raise ValueError
    if operation not in {"open", "pulse", "upgrade", "close"}:
        raise ValueError
    requests = GatewayRequest.objects.filter(pk=identifier)
    existing = requests.select_related("instance").first()
    if existing and (existing.instance.host != host or existing.kind != kind):
        raise PermissionDenied
    # Closing after revocation or route retirement must still release the reservation.
    if operation == "close":
        requests.delete()
        return {"allowed": True}
    decision = authorize(host, credential)
    if not decision["allowed"]:
        return decision
    instance = AppInstance.objects.get(pk=decision["instance_id"])
    if operation == "open":
        if existing:
            raise ValueError  # Never replay a forwarded request under an existing identity.
        if kind == "websocket" and instance.draining_for_id:
            return {"allowed": False, "reason": "draining"}
        # Safety bounds for abandoned requests, not app resource quotas or QoS.
        if GatewayRequest.objects.count() >= 4096 or instance.requests.count() >= 1024:
            raise TooManyConnections
        GatewayRequest.objects.create(id=identifier, instance=instance, kind=kind)
    else:
        if not existing or existing.instance_id != instance.pk:
            return {"allowed": False}
        if operation == "upgrade" and kind != "websocket":
            raise ValueError
        requests.update(
            heartbeat_at=timezone.now(),
            **({"upgraded": True} if operation == "upgrade" else {}),
        )
    return decision

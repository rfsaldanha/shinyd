from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_http_methods

from .gateway_auth import TooManyConnections, instance_origin, issue_ticket
from .models import AppInstance
from .permissions import visible_apps


@login_required
@require_http_methods(["GET", "POST"])
def connect(request, pk):
    instance = get_object_or_404(
        AppInstance.objects.select_related("app"),
        pk=pk,
        enabled=True,
        app__in=visible_apps(request.user),
    )
    try:
        origin = instance_origin(instance)
        # Host headers from other allowed administration aliases cannot mint handoffs.
        if request.build_absolute_uri("/").rstrip("/") != settings.SHINYD_PANEL_ORIGIN:
            return HttpResponse("Use o endereço configurado do painel.", status=403)
        if instance.draining_for_id:
            response = render(request, "control/connect.html", {"instance": instance}, status=503)
            response["Retry-After"] = "5"
            return response
        if request.method == "POST":
            code = issue_ticket(request.user, request.session.session_key, instance)
            response = render(
                request,
                "control/connect_handoff.html",
                {"instance": instance, "action": origin + "/_shinyd/connect", "code": code},
            )
            response["Content-Security-Policy"] = (
                "default-src 'none'; script-src 'self'; style-src 'self'; "
                "base-uri 'none'; frame-ancestors 'none'; form-action " + origin
            )
            # Fetch makes Origin null for form POSTs with no-referrer. Keep only
            # the origin; the one-use code remains exclusively in the POST body.
            response["Referrer-Policy"] = "strict-origin"
            return response
    except ValidationError:
        return HttpResponse("Conexão indisponível. Consulte o administrador.", status=503)
    except TooManyConnections:
        response = HttpResponse("Muitas conexões solicitadas. Aguarde um minuto.", status=429)
        response["Retry-After"] = "60"
        return response
    return render(request, "control/connect.html", {"instance": instance})

import json

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import render
from django.views.decorators.http import require_GET

from .models import AppInstance, Application, Job
from .views import administrator


@login_required
@require_GET
def readiness(request):
    administrator(request)
    receipt = None
    try:
        raw = json.loads((settings.STATE_DIR / "restore.json").read_text())
        receipt = {k: raw[k] for k in ("backup_id", "restored_at")}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return render(
        request,
        "control/backups.html",
        {
            "active_apps": Application.objects.filter(
                ~Q(desired_state="stopped") | Q(active_publication__isnull=False)
            ).count(),
            "pending_jobs": Job.objects.filter(status__in=["queued", "running"]).count(),
            "registered_routes": AppInstance.objects.filter(enabled=True).count(),
            "receipt": receipt,
        },
    )

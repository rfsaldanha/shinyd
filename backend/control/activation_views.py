import uuid

from django import forms
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from .activation_jobs import eligible, enqueue
from .models import Version
from .permissions import manageable_apps
from .services import Conflict


class ActivateForm(forms.Form):
    idempotency_key = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4)
    acknowledge = forms.BooleanField(
        label="Entendo que a troca interrompe as sessões da publicação anterior.",
        required=True,
    )


@login_required
@require_http_methods(["GET", "POST"])
def activate(request, pk):
    version = get_object_or_404(Version, pk=pk, app__in=manageable_apps(request.user))
    form = ActivateForm(request.POST or None)
    status, error = 200, ""
    try:
        eligible(version.app)
        if request.method == "POST" and form.is_valid():
            job = enqueue(request.user, version.pk, form.cleaned_data["idempotency_key"])
            return redirect("job_detail", pk=job.pk)
    except ValidationError as exc:
        error, status = " ".join(exc.messages), 409
    except Conflict as exc:
        form.add_error(None, str(exc))
        status = 409
    return render(
        request,
        "control/form.html",
        {
            "form": form,
            "service_error": error,
            "title": f"Ativar versão · {version.app.name}",
            "submit_label": "Ativar versão",
            "description": "A versão candidata será iniciada e verificada antes da troca. "
            "Falha ou falta de recursos preserva a publicação anterior. "
            "Após a troca, usuários conectados precisarão abrir o app novamente. "
            "Dados gravados em /tmp são temporários; persistência ainda não está disponível.",
        },
        status=status,
    )

import uuid

from django import forms
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from . import builder_client
from .build_jobs import enqueue
from .models import Version
from .permissions import manageable_apps
from .services import Conflict


class PrepareForm(forms.Form):
    environment = forms.ChoiceField(label="Ambiente R aprovado")
    idempotency_key = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4)


@login_required
@require_http_methods(["GET", "POST"])
def prepare(request, pk):
    version = get_object_or_404(Version, pk=pk, app__in=manageable_apps(request.user))
    form = PrepareForm(request.POST or None)
    status = 200
    try:
        catalog = builder_client.environments()
        form.fields["environment"].choices = [
            (item["id"], f"{item['id']} · R {item['r_version']}") for item in catalog
        ]
        if request.method == "POST" and form.is_valid():
            job = enqueue(
                request.user,
                version.pk,
                form.cleaned_data["environment"],
                form.cleaned_data["idempotency_key"],
            )
            return redirect("job_detail", pk=job.pk)
    except (builder_client.BuilderUnavailable, builder_client.BuilderRejected):
        status = 503
    except (Conflict, ValidationError) as exc:
        form.add_error(None, str(exc))
        status = 409
    return render(
        request,
        "control/form.html",
        {
            "form": form,
            "service_error": "Preparador indisponível. Consulte o administrador."
            if status == 503
            else "",
            "title": f"Preparar versão · {version.app.name}",
            "description": "Será usada uma biblioteca privada, com os pacotes do renv.lock já "
            "disponíveis no ambiente aprovado. Esta operação preserva a publicação atual.",
        },
        status=status,
    )

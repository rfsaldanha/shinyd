import uuid

from django import forms
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from .activation_jobs import eligible, enqueue
from .models import Publication, Version
from .permissions import manageable_apps
from .services import Conflict


class ActivateForm(forms.Form):
    idempotency_key = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4)
    switch_mode = forms.ChoiceField(
        label="Modo de troca",
        choices=Publication.SwitchMode.choices,
        initial="immediate",
        required=False,
        help_text="A troca imediata encerra conexões. A espera bloqueia novas conexões "
        "interativas e preserva a publicação anterior se exceder cinco minutos.",
    )
    acknowledge = forms.BooleanField(
        label="Entendo o impacto do modo de troca escolhido.",
        required=True,
    )

    def clean_switch_mode(self):
        return self.cleaned_data["switch_mode"] or "immediate"


@login_required
@require_http_methods(["GET", "POST"])
def activate(request, pk):
    version = get_object_or_404(Version, pk=pk, app__in=manageable_apps(request.user))
    form = ActivateForm(request.POST or None)
    status, error = 200, ""
    applying = (
        version.app.active_publication_id
        and version.app.active_publication.version_id == version.pk
    )
    try:
        eligible(version.app)
        if request.method == "POST" and form.is_valid():
            job = enqueue(
                request.user,
                version.pk,
                form.cleaned_data["idempotency_key"],
                switch_mode=form.cleaned_data["switch_mode"],
            )
            return redirect("job_detail", pk=job.pk)
    except ValidationError as exc:
        error, status = " ".join(exc.messages), 409
    except Conflict as exc:
        form.add_error(None, str(exc))
        status = 409
    title = "Aplicar configuração" if applying else "Ativar versão"
    description = (
        "A versão candidata será iniciada e verificada antes da troca. "
        "Falha ou falta de recursos preserva a publicação anterior. "
        "Escolha encerrar conexões na troca ou aguardar que os usuários fechem o app. "
        "Dados gravados em /tmp são temporários."
    )
    if version.app.data_enabled:
        description = (
            "A candidata será testada com dados temporários vazios. Se passar, o app anterior "
            "será interrompido antes da partida com os dados persistentes. "
            "Falha nessa partida deixa o app indisponível e não desfaz alterações nos dados. "
            "Mantenha backup antes de atualizar código que modifica dados. "
            "No modo de espera, a interrupção aguarda as conexões existentes terminarem."
        )
    return render(
        request,
        "control/form.html",
        {
            "form": form,
            "service_error": error,
            "title": f"{title} · {version.app.name}",
            "submit_label": "Aplicar configuração e reiniciar" if applying else "Ativar versão",
            "description": description,
        },
        status=status,
    )

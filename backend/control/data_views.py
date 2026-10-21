from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from .models import Application
from .permissions import manageable_apps
from .services import Conflict, enable_data


class DataForm(forms.Form):
    revision = forms.IntegerField(widget=forms.HiddenInput, min_value=1)
    acknowledge = forms.BooleanField(
        label="Entendo que as próximas ativações interrompem este app "
        "e que devo manter backup dos dados."
    )


@login_required
@require_http_methods(["GET", "POST"])
def enable(request, pk):
    app = get_object_or_404(Application.objects.filter(pk__in=manageable_apps(request.user)), pk=pk)
    form = DataForm(request.POST or None, initial={"revision": app.revision})
    status = 200
    if request.method == "POST" and form.is_valid():
        try:
            enable_data(request.user, pk, form.cleaned_data["revision"])
            messages.success(request, "Dados persistentes habilitados para a próxima ativação.")
            return redirect("app_detail", pk=pk)
        except Conflict as exc:
            form.add_error(None, str(exc))
            status = 409
    return render(
        request,
        "control/form.html",
        {
            "form": form,
            "title": f"Dados persistentes · {app.name}",
            "submit_label": "Habilitar dados persistentes",
            "description": 'O app deve gravar em Sys.getenv("SHINYD_DATA_DIR"). '
            "Arquivos em /tmp continuam temporários. A candidata será testada com uma pasta "
            "vazia e descartável; depois, a publicação anterior será encerrada antes da "
            "partida com dados reais. Falha nessa última partida deixa o app indisponível "
            "e não desfaz alterações nos dados. Um único processo pode usar o volume. "
            "Esta opção não apaga nem importa arquivos e não pode ser desabilitada pelo painel.",
        },
        status=status,
    )

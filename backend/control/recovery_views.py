from django import forms
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from . import lifecycle, recovery
from .models import RecoveryState
from .permissions import manageable_apps
from .services import Conflict

SALT = "shinyd.recovery.v1"


class PolicyForm(forms.Form):
    confirmation = forms.CharField(widget=forms.HiddenInput, max_length=4000)
    enabled = forms.BooleanField(
        label="Recuperar automaticamente após encerramento do processo", required=False
    )
    acknowledge = forms.BooleanField(
        label="Entendo que as sessões encerradas não serão recuperadas e que o app poderá "
        "executar novamente seu código de inicialização e alterar dados persistentes."
    )


@login_required
@require_http_methods(["GET", "POST"])
def configure(request, pk):
    app = get_object_or_404(manageable_apps(request.user), pk=pk)
    row = RecoveryState.objects.filter(app=app).first()
    initial = {
        "enabled": row.enabled if row else False,
        "confirmation": signing.dumps(
            {
                "actor": request.user.pk,
                "app": lifecycle.state(app),
                "revision": row.revision if row else 0,
            },
            salt=SALT,
        ),
    }
    form = PolicyForm(request.POST if request.method == "POST" else None, initial=initial)
    status = 200
    if request.method == "POST" and form.is_valid():
        try:
            payload = signing.loads(form.cleaned_data["confirmation"], salt=SALT, max_age=1800)
            with transaction.atomic():
                app = get_object_or_404(manageable_apps(request.user), pk=pk)
                if payload["actor"] != request.user.pk or payload["app"] != lifecycle.state(app):
                    raise Conflict(
                        "O app mudou ou a confirmação pertence a outra conta. "
                        "Reabra a configuração."
                    )
                recovery.configure(
                    request.user, pk, payload["revision"], form.cleaned_data["enabled"]
                )
            return redirect("app_detail", pk=pk)
        except (signing.BadSignature, Conflict) as exc:
            form.add_error(
                None, str(exc) if isinstance(exc, Conflict) else "Confirmação inválida ou expirada."
            )
            status = 409
    return render(
        request,
        "control/form.html",
        {
            "form": form,
            "title": f"Recuperação de falhas · {app.name}",
            "description": "Até três tentativas automáticas, com esperas de 10, 30 e 60 segundos. "
            "Falhas repetidas suspendem a recuperação até intervenção. Dez minutos de execução "
            "estável observada zeram a sequência; o limite de três tentativas "
            "em dez minutos permanece. "
            "Falta de memória, perda de permissão ou alterações pendentes exigem intervenção. "
            "Desabilitar cancela tentativas ainda não ativadas e mantém a limpeza necessária. "
            "Salvar habilitado também rearma uma recuperação suspensa.",
        },
        status=status,
    )

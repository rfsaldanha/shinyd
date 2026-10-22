import uuid

from django import forms
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods

from . import connections, lifecycle
from .models import OperationBatch, Publication
from .permissions import is_admin, manageable_apps
from .services import Conflict

SALT = "shinyd.operations.v1"


class SelectionForm(forms.Form):
    action = forms.ChoiceField(label="Operação", choices=OperationBatch.Action.choices)
    switch_mode = forms.ChoiceField(
        label="Modo de parada ou reinício",
        choices=Publication.SwitchMode.choices,
        initial="drain",
    )
    apps = forms.ModelMultipleChoiceField(
        label="Aplicativos", queryset=None, required=False, widget=forms.CheckboxSelectMultiple
    )
    all_apps = forms.BooleanField(
        label="Todos os aplicativos sob minha gestão neste momento",
        required=False,
    )

    def __init__(self, actor, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["apps"].queryset = manageable_apps(actor)

    def clean(self):
        result = super().clean()
        if "apps" not in result:
            return result
        selected = self.fields["apps"].queryset if result.get("all_apps") else result["apps"]
        selected = list(selected[:101])
        if not 1 <= len(selected) <= 100:
            raise forms.ValidationError(
                "Escolha de um a cem apps. Divida conjuntos maiores em seleções."
            )
        result["targets"] = [lifecycle.state(app) for app in selected]
        return result


class ConfirmationForm(forms.Form):
    confirmation = forms.CharField(widget=forms.HiddenInput, max_length=64000)
    acknowledge = forms.BooleanField(
        label="Entendo o impacto sobre as conexões, os dados temporários "
        "e a configuração dos apps listados."
    )

    def clean_confirmation(self):
        try:
            return signing.loads(self.cleaned_data["confirmation"], salt=SALT, max_age=1800)
        except signing.BadSignature as exc:
            raise forms.ValidationError(
                "Confirmação inválida ou expirada. Selecione os apps novamente."
            ) from exc


def rows(actor, targets):
    apps = {
        str(app.pk): app
        for app in manageable_apps(actor).filter(pk__in=[target["app"] for target in targets])
    }
    return [
        {
            "app": apps[target["app"]],
            "connections": connections.counts(target["active"]) if target["active"] else None,
            "state": target,
        }
        for target in targets
        if target["app"] in apps
    ]


@login_required
@require_http_methods(["GET", "POST"])
def operations(request):
    form = SelectionForm(
        request.user,
        request.POST if request.method == "POST" else None,
        initial={"apps": request.GET.getlist("app")},
    )
    confirmation, payload, error, status = None, None, "", 200
    if request.method == "POST" and "confirmation" in request.POST:
        confirmation = ConfirmationForm(request.POST)
        if confirmation.is_valid():
            payload = confirmation.cleaned_data["confirmation"]
            if payload["actor"] != request.user.pk:
                confirmation.add_error(None, "Esta confirmação pertence a outra conta.")
                status = 409
            else:
                try:
                    batch = lifecycle.enqueue(
                        request.user,
                        payload["action"],
                        payload["mode"],
                        payload["key"],
                        payload["targets"],
                    )
                    return redirect("operation_detail", pk=batch.pk)
                except (Conflict, ValidationError) as exc:
                    error = " ".join(exc.messages) if isinstance(exc, ValidationError) else str(exc)
                    status = 409
        else:
            status = 400
            # Keep the concrete impact visible if only the acknowledgement was omitted.
            payload = confirmation.cleaned_data.get("confirmation")
    elif request.method == "POST" and form.is_valid():
        payload = {
            "actor": request.user.pk,
            "key": str(uuid.uuid4()),
            "action": form.cleaned_data["action"],
            "mode": form.cleaned_data["switch_mode"],
            "targets": form.cleaned_data["targets"],
        }
        confirmation = ConfirmationForm(initial={"confirmation": signing.dumps(payload, salt=SALT)})
    return render(
        request,
        "control/operations.html",
        {
            "form": form,
            "confirmation": confirmation,
            "service_error": error,
            "rows": rows(request.user, payload["targets"]) if payload else [],
            "action_label": OperationBatch.Action(payload["action"]).label if payload else "",
            "mode_label": Publication.SwitchMode(payload["mode"]).label if payload else "",
            "action": payload["action"] if payload else "",
            "mode": payload["mode"] if payload else "",
        },
        status=status,
    )


@login_required
@require_GET
def detail(request, pk):
    batches = OperationBatch.objects.all()
    if not is_admin(request.user):
        batches = batches.filter(actor=request.user)
    batch = get_object_or_404(batches, pk=pk)
    items = list(
        batch.items.filter(app__in=manageable_apps(request.user)).select_related("app", "job")
    )
    for item in items:
        item.result_status = (
            item.job.get_status_display()
            if item.job_id
            else ("Concluído" if item.status == "succeeded" else "Não enviado")
        )
        item.result_message = item.job.message if item.job_id else item.message
    return render(
        request,
        "control/operation.html",
        {
            "batch": batch,
            "items": items,
            "pending": any(
                item.job_id and item.job.status in {"queued", "running"} for item in items
            ),
            "hidden": batch.items.count() - len(items),
        },
    )

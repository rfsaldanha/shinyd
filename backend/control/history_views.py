"""Publisher-only history and explicit, stale-safe code rollback."""

from django import forms
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods

from .activation_jobs import eligible, enqueue, rollback_state
from .activation_views import ActivateForm
from .models import Job, Publication
from .permissions import manageable_apps
from .services import Conflict

CONFIRMATION_SALT = "shinyd.rollback.v1"
VERSION_STATES = {
    "received": "Recebido",
    "validated": "Validado",
    "invalid": "Inválido",
    "cancelled": "Inspeção cancelada",
    "build_queued": "Na fila de preparação",
    "prepared": "Preparado",
    "build_failed": "Falha na preparação",
    "build_cancelled": "Preparação cancelada",
}
PUBLICATION_STATES = {
    "candidate": "Em verificação",
    "active": "Ativa",
    "retiring": "Encerrando",
    "retired": "Encerrada",
    "failed": "Falhou",
    "cancelled": "Cancelada",
}


class RollbackForm(ActivateForm):
    confirmation = forms.CharField(widget=forms.HiddenInput, max_length=2000)
    acknowledge_data = forms.BooleanField(
        label="Entendo que voltar o código não desfaz alterações nos dados e usa a "
        "configuração atual, incluindo alterações pendentes e segredos.",
    )

    def clean_confirmation(self):
        try:
            return signing.loads(
                self.cleaned_data["confirmation"], salt=CONFIRMATION_SALT, max_age=1800
            )
        except signing.BadSignature as exc:
            raise forms.ValidationError(
                "Confirmação inválida ou expirada. Reabra esta página."
            ) from exc


@login_required
@require_GET
def history(request, pk):
    app = get_object_or_404(
        manageable_apps(request.user).select_related("active_publication"), pk=pk
    )
    versions = Paginator(
        app.versions.defer("manifest", "build_manifest", "source_path")
        .select_related("created_by")
        .order_by("-created_at", "-pk"),
        20,
    ).get_page(request.GET.get("versions"))
    publications = Paginator(
        Publication.objects.filter(app=app)
        .select_related("created_by")
        .defer("environment_snapshot", "runtime_spec")
        .order_by("-created_at", "-pk"),
        20,
    ).get_page(request.GET.get("publications"))
    jobs = {
        job.publication_id: job
        for job in Job.objects.filter(
            publication_id__in=[p.pk for p in publications], kind="activate_version"
        ).only("id", "publication_id")
    }
    for version in versions:
        version.state_label = VERSION_STATES.get(version.status, "Em processamento")
    for publication in publications:
        publication.state_label = PUBLICATION_STATES.get(publication.status, "Desconhecido")
        publication.job = jobs.get(publication.pk)
    return render(
        request,
        "control/history.html",
        {"app": app, "versions": versions, "publications": publications},
    )


@login_required
@require_http_methods(["GET", "POST"])
def rollback(request, pk):
    source = get_object_or_404(
        Publication.objects.select_related("app__active_publication", "version")
        .defer("environment_snapshot", "runtime_spec")
        .filter(app__in=manageable_apps(request.user)),
        pk=pk,
    )
    app = source.app
    form = RollbackForm(
        request.POST if request.method == "POST" else None,
        initial={
            "confirmation": signing.dumps(rollback_state(app, source), salt=CONFIRMATION_SALT)
        },
    )
    status, error = 200, ""
    try:
        eligible(app)
        if source.status != "retired":
            raise ValidationError("Escolha uma publicação anterior encerrada deste app.")
        if request.method == "POST" and form.is_valid():
            job = enqueue(
                request.user,
                source.version_id,
                form.cleaned_data["idempotency_key"],
                restored_from=source.pk,
                expected_state=form.cleaned_data["confirmation"],
                switch_mode=form.cleaned_data["switch_mode"],
            )
            return redirect("job_detail", pk=job.pk)
    except ValidationError as exc:
        error, status = " ".join(exc.messages), 409
    except Conflict as exc:
        form.add_error(None, str(exc))
        status = 409
    description = (
        f"Será reutilizada a imagem preparada da versão {source.version_id}, "
        f"com a configuração atual na revisão {app.config_revision}. "
        "Variáveis removidas não serão recuperadas e segredos antigos não serão restaurados. "
        "Escolha encerrar as conexões ou aguardar seu encerramento após verificar a candidata. "
    )
    if app.data_enabled:
        description += (
            "O teste inicial usa dados temporários vazios. Depois dele, a instância anterior "
            "será removida antes de abrir os dados persistentes. Falha nessa partida deixa "
            "o app indisponível. Voltar o código não reverte escritas ou migrações nos dados: "
            "confirme a compatibilidade da versão e mantenha backup."
        )
    else:
        description += "Os arquivos temporários da instância anterior serão descartados."
    return render(
        request,
        "control/form.html",
        {
            "form": form,
            "service_error": error,
            "title": f"Voltar a esta versão · {app.name}",
            "submit_label": "Confirmar retorno à versão",
            "description": description,
        },
        status=status,
    )

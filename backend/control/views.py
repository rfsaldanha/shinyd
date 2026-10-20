import hashlib
import hmac
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_GET, require_POST

from .forms import AppForm, EditUserForm, EnvironmentForm, GrantForm, NewUserForm, UploadForm
from .models import AppGrant, AuditEvent, Job, LoginAttempt, PriorityGroup, User
from .permissions import can_create, is_admin, manageable_apps, visible_apps
from .services import (
    Conflict,
    create_app,
    event,
    receive_upload,
    remove_environment,
    set_environment,
    update_app,
)


def administrator(request):
    if not is_admin(request.user):
        raise PermissionDenied


class Login(auth_views.LoginView):
    template_name = "control/login.html"

    def buckets(self):
        raw = [
            ("account:" + self.request.POST.get("username", "").strip().casefold(), 5),
            ("address:" + self.request.META.get("REMOTE_ADDR", ""), 50),
        ]
        return [
            (hmac.new(settings.SECRET_KEY.encode(), v.encode(), hashlib.sha256).hexdigest(), n)
            for v, n in raw
        ]

    def post(self, request, *args, **kwargs):
        for key, limit in self.buckets():
            if LoginAttempt.objects.filter(
                key=key, expires_at__gt=timezone.now(), failures__gte=limit
            ).exists():
                response = render(
                    request,
                    self.template_name,
                    {
                        "form": self.get_form(),
                        "throttled": (
                            "Muitas tentativas. Aguarde 15 minutos antes de tentar novamente."
                        ),
                    },
                    status=429,
                )
                response["Retry-After"] = "900"
                return response
        return super().post(request, *args, **kwargs)

    def form_invalid(self, form):
        with transaction.atomic():
            for key, _ in self.buckets():
                attempt, _ = LoginAttempt.objects.get_or_create(
                    key=key, defaults={"expires_at": timezone.now() + timedelta(minutes=15)}
                )
                if attempt.expires_at <= timezone.now():
                    attempt.failures = 0
                    attempt.expires_at = timezone.now() + timedelta(minutes=15)
                attempt.failures += 1
                attempt.save()
        return super().form_invalid(form)

    def form_valid(self, form):
        LoginAttempt.objects.filter(key=self.buckets()[0][0]).delete()
        return super().form_valid(form)


class Recover(auth_views.PasswordResetConfirmView):
    template_name = "control/recover.html"
    success_url = "/login/"

    def form_valid(self, form):
        response = super().form_valid(form)
        event(self.user, "user.password_recovered", self.user.pk)
        return response


@require_GET
def health(request):
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()
    return JsonResponse({"status": "ok", "component": "control", "runtime_ready": False})


@login_required
@require_GET
def dashboard(request):
    apps = visible_apps(request.user).select_related("priority", "owner")
    return render(
        request,
        "control/dashboard.html",
        {
            "apps": apps,
            "can_create": can_create(request.user),
            "managed_count": manageable_apps(request.user).count(),
            "active_jobs": Job.objects.filter(
                app__in=manageable_apps(request.user), status__in=["queued", "running"]
            ).count(),
        },
    )


@login_required
def app_create(request):
    if not can_create(request.user):
        raise PermissionDenied
    form = AppForm(
        request.POST or None,
        initial={"priority": PriorityGroup.objects.filter(slug="normal").first()},
    )
    if request.method == "POST" and form.is_valid():
        values = {k: v for k, v in form.cleaned_data.items() if k != "revision"}
        app = create_app(request.user, values)
        messages.success(
            request, "Aplicativo cadastrado. Envie o ZIP para inspecionar a publicação."
        )
        return redirect("app_detail", pk=app.pk)
    return render(request, "control/form.html", {"form": form, "title": "Novo aplicativo"})


@login_required
@require_GET
def app_detail(request, pk):
    app = get_object_or_404(visible_apps(request.user).select_related("priority", "owner"), pk=pk)
    manage = manageable_apps(request.user).filter(pk=pk).exists()
    return render(
        request,
        "control/app.html",
        {
            "app": app,
            "manage": manage,
            "versions": app.versions.all()[:20] if manage else [],
            "jobs": Job.objects.filter(app=app).order_by("-created_at")[:20] if manage else [],
            "environment": app.environment.defer("ciphertext").all() if manage else [],
            "grants": app.grants.select_related("user", "group").all() if manage else [],
            "active_instance": app.instances.filter(
                publication_id=app.active_publication_id, enabled=True
            ).first()
            if app.active_publication_id
            else None,
        },
    )


@login_required
def app_edit(request, pk):
    app = get_object_or_404(manageable_apps(request.user), pk=pk)
    form = AppForm(request.POST or None, instance=app, initial={"revision": app.revision})
    status = 200
    if request.method == "POST" and form.is_valid():
        try:
            update_app(
                request.user,
                pk,
                form.cleaned_data["revision"],
                {k: v for k, v in form.cleaned_data.items() if k != "revision"},
            )
            messages.success(request, "Configuração atualizada.")
            return redirect("app_detail", pk=pk)
        except Conflict as exc:
            form.add_error(None, str(exc))
            status = 409
    return render(
        request,
        "control/form.html",
        {"form": form, "title": "Configurar aplicativo"},
        status=status,
    )


@login_required
def app_access(request, pk):
    app = get_object_or_404(manageable_apps(request.user), pk=pk)
    form = GrantForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            grant, _ = AppGrant.objects.update_or_create(
                app=app,
                user=form.cleaned_data["user"],
                group=form.cleaned_data["group"],
                defaults={"role": form.cleaned_data["role"]},
            )
            event(request.user, "app.access_granted", pk, grant=grant.pk, role=grant.role)
        messages.success(request, "Permissão atualizada.")
        return redirect("app_detail", pk=pk)
    return render(request, "control/form.html", {"form": form, "title": "Conceder acesso"})


@login_required
@sensitive_post_parameters("value")
def app_config(request, pk):
    app = get_object_or_404(manageable_apps(request.user), pk=pk)
    form = EnvironmentForm(request.POST or None, initial={"revision": app.config_revision})
    status = 200
    if request.method == "POST" and form.is_valid():
        try:
            set_environment(
                request.user,
                pk,
                form.cleaned_data["revision"],
                form.cleaned_data["name"],
                form.cleaned_data["value"],
                form.cleaned_data["is_secret"],
            )
            messages.success(
                request,
                "Valor salvo. Aplique a configuração para reiniciar o app com a nova revisão.",
            )
            return redirect("app_detail", pk=pk)
        except Conflict as exc:
            form.add_error(None, str(exc))
            status = 409
        except (ValidationError, OSError):
            form.add_error(None, "Não foi possível cifrar o valor. Verifique a chave e permissões.")
            status = 503
    return render(
        request,
        "control/form.html",
        {
            "form": form,
            "title": "Configuração de ambiente",
            "description": "O valor não será exibido novamente. Trocas exigirão reinício do app.",
        },
        status=status,
    )


@login_required
def environment_remove(request, pk, name):
    app = get_object_or_404(manageable_apps(request.user), pk=pk)
    from django import forms

    class RemoveForm(forms.Form):
        revision = forms.IntegerField(widget=forms.HiddenInput, min_value=0)
        acknowledge = forms.BooleanField(label="Confirmo a remoção desta variável.")

    form = RemoveForm(request.POST or None, initial={"revision": app.config_revision})
    status = 200
    if request.method == "POST" and form.is_valid():
        try:
            remove_environment(request.user, pk, form.cleaned_data["revision"], name)
            messages.success(
                request, "Variável removida. Aplique a configuração para reiniciar o app."
            )
            return redirect("app_detail", pk=pk)
        except Conflict as exc:
            form.add_error(None, str(exc))
            status = 409
    return render(
        request,
        "control/form.html",
        {
            "form": form,
            "title": f"Remover variável · {name}",
            "description": "A remoção será aplicada na próxima ativação. "
            "O processo atual mantém o valor.",
            "submit_label": "Remover variável",
        },
        status=status,
    )


@login_required
def upload(request, pk):
    app = get_object_or_404(manageable_apps(request.user), pk=pk)
    form = UploadForm(request.POST or None, request.FILES or None)
    status = 200
    if request.method == "POST" and form.is_valid():
        try:
            job = receive_upload(
                request.user,
                pk,
                form.cleaned_data["archive"],
                form.cleaned_data["idempotency_key"],
                form.cleaned_data["entrypoint"],
            )
            return redirect("job_detail", pk=job.pk)
        except Conflict as exc:
            form.add_error(None, str(exc))
            status = 409
        except ValidationError as exc:
            form.add_error(None, exc)
            status = 400
    return render(
        request,
        "control/form.html",
        {
            "form": form,
            "title": f"Enviar ZIP · {app.name}",
            "description": "Nesta etapa, o ZIP será inspecionado sem executar R. "
            "Após validar, prepare a versão e solicite sua ativação.",
        },
        status=status,
    )


def accessible_job(request, pk):
    return get_object_or_404(
        Job.objects.select_related("app", "version").filter(app__in=manageable_apps(request.user)),
        pk=pk,
    )


@login_required
@require_GET
def job_detail(request, pk):
    return render(request, "control/job.html", {"job": accessible_job(request, pk)})


@login_required
@require_POST
def job_cancel(request, pk):
    job = accessible_job(request, pk)
    with transaction.atomic():
        if job.kind == "activate_version" and job.publication_id:
            from .models import Publication

            if Publication.objects.get(pk=job.publication_id).status == "active":
                messages.info(
                    request, "A troca já foi concluída. O encerramento anterior continuará."
                )
                return redirect("job_detail", pk=pk)
        if Job.objects.filter(pk=job.pk, status__in=["queued", "running"]).update(
            cancel_requested=True
        ):
            Job.objects.filter(pk=job.pk, status="queued", kind="inspect_upload").update(
                status="cancelled", phase="cancelled", message="Inspeção cancelada."
            )
            event(request.user, "job.cancel_requested", job.pk)
    return redirect("job_detail", pk=pk)


@login_required
@require_GET
def users(request):
    administrator(request)
    return render(request, "control/users.html", {"accounts": User.objects.order_by("username")})


@login_required
@sensitive_post_parameters("password1", "password2")
def user_create(request):
    administrator(request)
    form = NewUserForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            account = form.save()
            event(request.user, "user.created", account.pk)
        return redirect("users")
    return render(request, "control/form.html", {"form": form, "title": "Nova conta"})


@login_required
def user_edit(request, pk):
    administrator(request)
    account = get_object_or_404(User, pk=pk)
    form = EditUserForm(request.POST or None, instance=account)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            active_admins = User.objects.filter(is_superuser=True, is_active=True).count()
            if account.is_superuser and not form.cleaned_data["is_active"] and active_admins <= 1:
                form.add_error("is_active", "Não é possível desativar o último administrador.")
            else:
                if "is_active" in form.changed_data:
                    form.instance.session_revision += 1
                form.save()
                event(request.user, "user.updated", account.pk, fields=sorted(form.changed_data))
                return redirect("users")
    return render(
        request, "control/form.html", {"form": form, "title": f"Conta · {account.username}"}
    )


@login_required
@require_POST
def recovery_issue(request, pk):
    administrator(request)
    user = get_object_or_404(User, pk=pk, is_active=True)
    token = default_token_generator.make_token(user)
    path = reverse(
        "recover", kwargs={"uidb64": urlsafe_base64_encode(force_bytes(user.pk)), "token": token}
    )
    event(request.user, "user.recovery_issued", user.pk)
    return render(request, "control/recovery_issued.html", {"recovery_path": path, "account": user})


@login_required
@require_GET
def audit(request):
    administrator(request)
    return render(
        request, "control/audit.html", {"events": AuditEvent.objects.select_related("actor")[:200]}
    )


@login_required
@require_GET
def api_apps(request):
    return JsonResponse(
        {
            "apps": [
                {
                    "id": str(a.pk),
                    "name": a.name,
                    "slug": a.slug,
                    "visibility": a.visibility,
                    "priority": a.priority.slug,
                    "desired_state": a.desired_state,
                    "revision": a.revision,
                    "runtime_ready": False,
                }
                for a in visible_apps(request.user).select_related("priority")
            ]
        }
    )


@login_required
@require_GET
def api_job(request, pk):
    job = accessible_job(request, pk)
    return JsonResponse(
        {
            "id": str(job.pk),
            "status": job.status,
            "phase": job.phase,
            "message": job.message,
            "attempts": job.attempts,
            "cancel_requested": job.cancel_requested,
        }
    )

import re
import uuid

from django import forms
from django.contrib.auth.forms import UserCreationForm

from .models import AppGrant, Application, User


class AppForm(forms.ModelForm):
    revision = forms.IntegerField(widget=forms.HiddenInput, required=False)

    class Meta:
        model = Application
        fields = ["name", "slug", "description", "visibility", "priority", "revision"]
        labels = {"priority": "Grupo de prioridade"}

    def clean_slug(self):
        slug = self.cleaned_data["slug"]
        if slug in {"admin", "api", "login", "painel", "static", "www", "auth"}:
            raise forms.ValidationError("Endereço reservado.")
        return slug


class NewUserForm(UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = User
        fields = ["username", "email", "first_name", "last_name", "can_publish"]


class EditUserForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ["email", "first_name", "last_name", "is_active", "can_publish", "groups"]


class GrantForm(forms.ModelForm):
    class Meta:
        model = AppGrant
        fields = ["user", "group", "role"]
        labels = {"user": "Usuário", "group": "Grupo de usuários", "role": "Permissão"}

    def clean(self):
        data = super().clean()
        if bool(data.get("user")) == bool(data.get("group")):
            raise forms.ValidationError("Escolha exatamente um usuário ou um grupo.")
        return data


class EnvironmentForm(forms.Form):
    revision = forms.IntegerField(widget=forms.HiddenInput, min_value=0)
    name = forms.RegexField(r"^[A-Z][A-Z0-9_]*$", label="Nome", max_length=128)
    value = forms.CharField(
        label="Novo valor",
        max_length=16384,
        strip=False,
        widget=forms.PasswordInput(render_value=False),
    )
    is_secret = forms.BooleanField(label="Segredo", initial=True, required=False)

    def clean_name(self):
        name = self.cleaned_data["name"]
        if name.startswith(("SHINYD_", "LD_", "DYLD_", "R_", "RENV_")) or name in {
            "PATH",
            "HOME",
            "USER",
            "SHELL",
            "TMPDIR",
            "PYTHONPATH",
            "BASH_ENV",
            "ENV",
            "NODE_OPTIONS",
            "IFS",
        }:
            raise forms.ValidationError("Nome reservado ao ambiente de execução.")
        return name

    def clean_value(self):
        value = self.cleaned_data["value"]
        if "\x00" in value:
            raise forms.ValidationError("O valor não pode conter NUL.")
        return value


class UploadForm(forms.Form):
    archive = forms.FileField(label="Aplicativo em ZIP")
    idempotency_key = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4)
    entrypoint = forms.CharField(
        label="Pasta do app dentro do ZIP (opcional)",
        max_length=200,
        required=False,
        help_text="Deixe vazio para detectar automaticamente. Use . para a raiz.",
    )

    def clean_entrypoint(self):
        path = self.cleaned_data["entrypoint"]
        if (
            path
            and path != "."
            and not re.fullmatch(r"[A-Za-z0-9_. -]+(?:/[A-Za-z0-9_. -]+)*", path)
        ):
            raise forms.ValidationError("Caminho relativo inválido.")
        if ".." in path.split("/"):
            raise forms.ValidationError("Caminho relativo inválido.")
        return path

"""Bounded, encrypted publication snapshots; plaintext stays at the runtime boundary."""

import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.core.exceptions import ValidationError
from django.views.decorators.debug import sensitive_variables

from builder.policy import canonical, strict_json

from .forms import EnvironmentForm
from .services import decrypt_environment, master_key


def validate(values):
    if not isinstance(values, dict) or len(values) > 64:
        raise ValidationError("Use até 64 variáveis por app.")
    total = 0
    for name, value in values.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise ValidationError("Ambiente inválido.")
        form = EnvironmentForm({"revision": 0, "name": name, "value": value})
        if not form.is_valid() or form.cleaned_data["name"] != name:
            raise ValidationError("Nome ou valor de ambiente inválido.")
        total += len(name.encode()) + len(value.encode())
    if total > 65536:
        raise ValidationError("O ambiente completo deve ocupar até 64 KiB em UTF-8.")


def aad(publication):
    return (
        f"publication:{publication.pk}:{publication.app_id}:{publication.config_revision}".encode()
    )


@sensitive_variables()
def snapshot(publication):
    if publication.config_revision == 0:
        return b""
    try:
        values = {v.name: decrypt_environment(v) for v in publication.app.environment.all()}
        validate(values)
        nonce = os.urandom(12)
        return nonce + AESGCM(master_key()).encrypt(nonce, canonical(values), aad(publication))
    except (InvalidTag, OSError, ValueError, UnicodeError) as exc:
        raise ValidationError(
            "Não foi possível ler a configuração cifrada. Verifique a chave."
        ) from exc


@sensitive_variables()
def reveal(publication):
    if publication.config_revision == 0:
        return None
    raw = bytes(publication.environment_snapshot)
    values = strict_json(AESGCM(master_key()).decrypt(raw[:12], raw[12:], aad(publication)))
    validate(values)
    return values

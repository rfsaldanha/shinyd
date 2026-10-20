import getpass
import os

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from control.models import User
from control.services import event


class Command(BaseCommand):
    help = "Recuperação local de administrador; exige root e não imprime senhas/tokens."

    def add_arguments(self, parser):
        parser.add_argument("username")

    def handle(self, *args, **options):
        if os.geteuid() != 0:
            raise CommandError("Recuperação de administrador exige execução local como root.")
        try:
            user = User.objects.get(username=options["username"], is_superuser=True)
        except User.DoesNotExist as exc:
            raise CommandError("Administrador não encontrado.") from exc
        password = getpass.getpass("Nova senha: ")
        if password != getpass.getpass("Confirme a senha: "):
            raise CommandError("Senhas diferentes.")
        try:
            validate_password(password, user)
        except ValidationError as exc:
            raise CommandError(" ".join(exc.messages)) from exc
        with transaction.atomic():
            user.set_password(password)
            user.is_active = True
            user.save(update_fields=["password", "is_active"])
            event(None, "admin.recovered_locally", user.pk)
        self.stdout.write("Acesso recuperado. Sessões anteriores serão invalidadas.")

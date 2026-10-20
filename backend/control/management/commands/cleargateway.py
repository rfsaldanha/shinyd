from django.core.management.base import BaseCommand
from django.utils import timezone

from control.models import GatewaySession, GatewayTicket


class Command(BaseCommand):
    help = "Remove credenciais expiradas do gateway sem exibir seus valores."

    def handle(self, *args, **options):
        now = timezone.now()
        tickets, _ = GatewayTicket.objects.filter(expires_at__lte=now).delete()
        sessions, _ = GatewaySession.objects.filter(expires_at__lte=now).delete()
        self.stdout.write(f"Removidos: {tickets} tickets e {sessions} sessões expiradas.")

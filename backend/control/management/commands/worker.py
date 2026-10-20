import signal
import time

from django.core.management.base import BaseCommand

from control.jobs import process_one


class Command(BaseCommand):
    help = "Inspeciona ZIPs e acompanha preparação, ativação e limpeza de versões."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Processa no máximo um trabalho")

    def handle(self, *args, **options):
        stopping = False

        def stop(signum, frame):
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        while not stopping:
            processed = process_one()
            if options["once"]:
                self.stdout.write(
                    "Trabalho processado." if processed else "Nenhum trabalho na fila."
                )
                break
            if not processed:
                time.sleep(1)

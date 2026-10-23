"""Fixed progress messages on stderr; stdout remains the machine-readable report."""

import sys
from datetime import UTC, datetime

STAGES = {
    "builder_start": "Conferindo a instalação de teste e os pacotes.",
    "environments": "Preparando ambientes R e imagens de teste.",
    "installed_start": "Iniciando os testes pelo painel HTTPS e navegador.",
    "persistent_data": "Verificando dados persistentes, rollback, drenagem e recuperação.",
    "backup_start": "Preparando o ensaio de backup e parando os apps pelo painel.",
    "backup_create": "Criando o backup criptografado; esta etapa exporta as imagens.",
    "backup_verify": "Verificando todo o backup e as chaves de recuperação.",
    "backup_restore": "Restaurando na instalação vazia e carregando as imagens.",
    "backup_browser": "Conferindo o app restaurado, seus dados e segredos no navegador.",
    "cleanup": "Limpando os recursos de teste e preparando o relatório final.",
}


def progress(stage):
    print(f"[{datetime.now(UTC):%H:%M:%S} UTC] {STAGES[stage]}", file=sys.stderr, flush=True)

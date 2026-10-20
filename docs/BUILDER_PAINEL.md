# Preparação de versões pelo painel

Este marco integra a preparação offline ao painel e ao worker. Depois de
inspecionar um ZIP, o publicador pode selecionar **Preparar versão**, escolher
um ambiente R aprovado e acompanhar o resultado. Ainda não ativa uma publicação:
o executor precisa de um contrato para reconhecer artefatos do builder antes
de automatizar partida, readiness e registro no gateway.

## Fluxo e autorização

1. O upload existente armazena o ZIP e a inspeção registra checksum e pasta.
2. A tela consulta os identificadores e versões R do catálogo protegido.
3. O POST cria um trabalho `prepare_version`, limitado pela fila global de
   100 trabalhos pendentes. O identificador de envio é idempotente.
4. O worker consulta primeiro o trabalho no builder. Caso não exista, transmite
   os bytes do ZIP com tamanho, checksum, UUID do app, pasta e ambiente.
5. O serviço confirma o hash, grava a solicitação e o ambiente resolvido
   antes de iniciar uma unidade systemd. O ambiente fica fixado para esse trabalho.
6. O worker acompanha as fases. Um resultado confirmado grava o manifesto e o
   ID imutável da imagem em `Version`; o estado passa a `prepared`.

As views conferem permissão de publicação sobre o app, inclusive para consultar
ou cancelar. O worker verifica novamente essa permissão ao sair da fila e ao
concluir. Revogação solicita cancelamento e impede registrar a imagem na versão.
As variáveis e os segredos do app não entram na solicitação nem no build.
`active_publication`, dados persistentes e instâncias existentes não são alterados.

## API privada

O socket `/run/shinyd-builder/control.sock` tem proprietário root e modo
`0660`, grupo `shinyd-control`. Além das permissões do arquivo, o serviço
exige o UID exato de `shinyd-control` via `SO_PEERCRED`. O cliente exige UID
root do outro lado, inclusive em desenvolvimento. Não há endpoint TCP.

| Operação | Contrato |
| --- | --- |
| `GET /v1/environments` | IDs e versões R dos ambientes aprovados |
| `PUT /v1/builds/{uuid}` | ZIP binário, `Content-Length` até 500 MiB e cabeçalho JSON `X-Shinyd-Build` |
| `GET /v1/builds/{uuid}` | Estado, fase, código de erro e manifesto quando concluído |
| `POST /v1/builds/{uuid}/cancel` | Pedido idempotente de cancelamento |

O cabeçalho JSON tem exatamente `app_id`, `source_hash`, `entrypoint` e
`environment`. Campos adicionais, caminhos externos, comandos, imagens e volumes
não são aceitos. Não aceita encoding chunked, parâmetros de URL ou upgrade.
O serviço limita conexões a oito handlers, headers aceitos a 8 KiB e upload
a 60 segundos, com timeout de leitura de três segundos. Não registra corpos,
cabeçalhos, saída de pacotes nem exceções.

O protocolo opera sobre HTTP apenas como transporte no socket local; não é um
servidor web exposto. A autorização por app permanece no serviço de controle:
comprometer a conta `shinyd-control` permite enviar builds aos ambientes aprovados,
mas não acrescentar opções de Podman ou escolher arquivos arbitrários do host.

## Serviços, recuperação e cancelamento

`shinyd-builder.service` atende a API na slice de controle, com limite de
192 MiB. Cada preparação é executada por `shinyd-build@{uuid}.service` na
slice de manutenção existente, sem reinício automático. A unidade tem prazo
máximo de 960 segundos; o builder conserva seus prazos menores por etapa.
Somente o UUID entra na chamada ao template instalado.

Os registros ficam em `/var/lib/shinyd-builder/jobs/{uuid}`. O serviço aceita
uma preparação por vez, sem fila adicional; os demais trabalhos aguardam no
banco do painel. A fila é FIFO neste marco; prioridades com envelhecimento
dependem da fase 4. O limite inicial é de 1.000 registros no builder, incluindo
cancelamentos, sem remoção automática. Ao atingir esse limite, novas entradas
são recusadas e o painel informa a espera. Gestão de retenção ainda é pendente.

Um reinício da API não encerra a unidade de build. Um worker novo consulta o
mesmo UUID; não cria outra instância de preparação após perder a resposta.
Leases de 90 segundos impedem que um worker antigo sobrescreva o resultado de
um novo. Consultas em andamento são espaçadas em três segundos.

Se a unidade de build morrer sem resultado, a API registra interrupção ao
consultá-la; não relança o build. Se houver intenção de limpeza pendente,
registra `cleanup`, preservando o estado para recuperação pelo próximo builder.
Reboot completo e morte forçada em todas as fases ainda exigem homologação.

Cancelar escreve uma marca durável. Extração, cópia e comandos longos verificam
essa marca; a interface aguarda confirmação. Cancelar antes de enviar cria um
registro terminal que impede um worker atrasado de iniciar o mesmo UUID.
Se conclusão e cancelamento concorrerem, o painel descarta o resultado quando
o cancelamento já estiver registrado. Uma imagem que acabou de ser selada pode
permanecer em cache, sem se tornar publicação. Limpeza malsucedida é falha
visível, não é apresentada como cancelamento concluído.

## Operação e demonstração

Atualize as dependências Python e aplique `manage.py migrate` antes de usar as
novas telas. A migração acrescenta dois campos e preserva versões existentes.
Para o contrato instalado, painel e worker precisam executar como
`shinyd-control`, com acesso ao seu estado privado. Uma conta comum de
desenvolvimento não é autorizada pela API.

O pacote `shinyd-builder 0.1.0~builder2` instala as unidades sem habilitar o
socket. O operador precisa aprovar o catálogo descrito em
[BUILDER_OFFLINE.md](BUILDER_OFFLINE.md), configurar o orçamento agregado do
executor e habilitar o socket quando preparar uma instalação de teste.
Não configure sudo para o publicador.

A demonstração privilegiada continua no roteiro:

```sh
python3 scripts/build_builder_package.py
set -o pipefail
sudo .venv/bin/python scripts/builder_smoke.py \
  --install-package 'dist/shinyd-builder_0.1.0~builder2_amd64.deb' \
  | tee var/builder-panel-report.json
```

As 33 verificações passaram no
[CI](https://github.com/rfsaldanha/shinyd/actions/runs/39463341316);
o [relatório](evidencias/builder-panel-ci.json) registra o commit e o ambiente
observado. Isso não substitui a homologação do novo pacote no host do operador.

Exige a instalação de teste vazia indicada pelo roteiro, os pacotes do agente
e executor, Chrome e o probe Go. O roteiro cria catálogo, banco, contas e ZIPs
descartáveis; testa as views HTTP e worker com o serviço real, reinicia a API,
confere imagem nova, cancelamento, permissões e ausência de segredo nas saídas
verificadas. Remove os registros da demonstração e encerra a API ao finalizar.
A navegação reativa dos dois apps R continua coberta pelo probe Chrome anterior;
a nova tela de preparação é exercitada pelo cliente de testes HTTP do Django.

Fontes: [systemd.service](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html),
[socket do Python](https://docs.python.org/3.12/library/socket.html),
[http.client](https://docs.python.org/3.12/library/http.client.html).

# Conexões e troca com espera — F3-04

O pacote `shinyd-control 0.1.0~control5`, com a migração `0010`, acrescenta
telemetria de conexões e drenagem às ativações, aplicações de configuração e
rollbacks. Executor `runtime4` e builder `builder2` permanecem compatíveis.
Evidências efetivamente executadas ficam em [STATUS.md](STATUS.md).

## Operação pela interface

Na confirmação da ativação ou retorno, escolha **Modo de troca**:

- **Imediata:** verifica a candidata e interrompe as conexões na troca, como
  nos marcos anteriores. O formulário exige confirmação de impacto.
- **Aguardar conexões:** verifica a candidata, bloqueia novas conexões
  WebSocket da publicação anterior e aguarda as existentes e as requisições
  HTTP em curso terminarem. A espera dura no máximo cinco minutos. Se o prazo
  terminar, a troca falha e a publicação anterior continua atendendo; não há
  encerramento forçado automático.

O contador na página do app diferencia WebSockets abertas, aberturas ainda
pendentes e requisições HTTP. A tarefa mostra a espera, seu início e o botão
de cancelamento. Cancelar reabre a admissão de conexões na publicação anterior
quando o worker processa o pedido, sem esperar a limpeza da candidata.

Uploads e downloads de usuários conectados continuam autorizados durante a
drenagem e também impedem a troca enquanto estiverem em curso. A entrada pelo
painel explica a espera. Uma tentativa de WebSocket por URL direta recebe 503;
a página do próprio app pode mostrar sua mensagem padrão de desconexão.
Cookies, permissões, revogação e afinidade continuam valendo durante a espera.

Com dados persistentes, a candidata é verificada em pasta temporária antes
da espera. A drenagem termina antes de desativar a rota e remover o escritor
anterior. Só então a nova instância abre o volume real. Falha nessa partida
pode deixar o app indisponível e não desfaz escritas; backup continua necessário.

## O que a contagem significa

O gateway observa conexões; não conta pessoas nem objetos de sessão dentro
do R. Um WebSocket só aparece como aberto após resposta HTTP 101 do app;
antes disso, sua reserva aparece como abertura pendente. Uma aba do fixture
Shiny nativo abre uma conexão; outros comportamentos precisam de homologação.
Contas autenticadas, visitas anônimas, duração de sessões e séries históricas
não são inferidas desses números e permanecem no trabalho de estatísticas.

A [documentação do objeto session do Shiny](https://shiny.posit.co/r/reference/shiny/1.14.0/session.html)
distingue a conexão WebSocket, callbacks de encerramento e opções de reconexão.
Este marco preserva o atendimento enquanto a conexão estiver aberta; não
espera tarefas R independentes, não certifica a conclusão de callbacks depois
da desconexão e não implementa restauração de sessão após queda de rede.
Nenhum callback fornecido pelo código do app pode autorizar a plataforma a
remover outro processo ou declarar uma conexão encerrada.

## Consistência e falhas

Cada requisição recebe no gateway um UUID aleatório que não vem do navegador.
Antes de encaminhar qualquer HTTP ou WebSocket, o gateway obtém autorização
e registra uma reserva por `/v2/request` no socket privado do autorizador.
Admissão, bloqueio de novas conexões e troca de rota usam transações SQLite
`IMMEDIATE`: observar zero conexões e desativar a rota é uma única transação.
Uma admissão anterior entra na contagem; uma admissão posterior à troca é
recusada. Não há intervalo livre entre consultar a contagem e retirar a rota.

Conexões longas renovam autorização e telemetria a cada 20 segundos. O gateway
só confirma encerramento ao terminar o encaminhamento, inclusive para HTTP
longo. Ausência de confirmação por mais de 60 segundos significa **incerteza**,
não zero conexões: a drenagem falha conservadoramente, preservando a publicação
anterior. Falha do autorizador fecha conexões conforme a política de revogação
existente; não significa preservação de sessão diante de falha de infraestrutura.

Se o gateway cair ou a confirmação de encerramento se perder, reservas podem
ficar pendentes. Não são apagadas por expiração. Uma troca imediata confirmada
pelo operador e a remoção confirmada do processo anterior permitem descartá-las.
Reiniciar somente o worker retoma a espera e seu prazo persistidos no banco.
O mesmo identificador de operação não pode ser reutilizado com outro modo de
troca. Pedidos de publicadores revogados ou alterações de configuração durante
a espera abortam a troca.

A tabela guarda IDs internos, tipo, estado e horários; não guarda URL, IP,
conteúdo, cookie ou segredo. Reservas encerradas são removidas. Há salvaguardas
de 4.096 reservas totais e 1.024 por instância para limitar estado abandonado;
esses números não são declaração de capacidade, cotas de CPU/RAM ou política
de QoS. Este contrato escreve no SQLite na admissão, renovação e encerramento;
seu custo sob concorrência precisa ser medido na Fase 4.

## Atualização e demonstração

Atualizar exige a janela descrita em [INSTALACAO_INTEGRADA.md](INSTALACAO_INTEGRADA.md):
parar o target, preservar estado/chaves, instalar o pacote, aplicar a migração e
iniciar todos os componentes. O autorizador recusa `/v1/authorize`, pois o
gateway anterior encaminhava requisições sem registrá-las. Não misture as
versões dos componentes. Reiniciar o gateway encerra conexões existentes.

Verificações locais sem privilégios:

```sh
.venv/bin/pytest -q tests/test_connection_drain.py tests/test_drain_concurrency.py
go test -race ./internal/gateway
```

O teste de concorrência usa dois acessos reais a um SQLite em arquivo, com
as opções de produção, nas duas ordens possíveis de admissão e troca. Os
testes de domínio cobrem acesso público/restrito, transferências, cancelamento,
prazo, incerteza, revogação, alteração de configuração e escritor persistente.

O roteiro instalado mantém uma sessão Shiny reativa durante a espera, recusa
outra conexão autorizada, faz upload/download, cancela e reabre a admissão,
reinicia o worker e conclui a troca após sair do app. Também confere o volume,
outro app ainda reativo e o painel em 360 px. Após construir os pacotes, execute
somente no ambiente de homologação vazio:

```sh
set -o pipefail
sudo .venv/bin/python scripts/builder_smoke.py --activation \
  --install-runtime-package dist/shinyd-runtime_0.1.0~runtime4_amd64.deb \
  --install-package dist/shinyd-builder_0.1.0~builder2_amd64.deb \
  --install-control-package dist/shinyd-control_0.1.0~control5_amd64.deb \
  | tee var/drain-report.json
```

O ensaio limpa os serviços e dados descartáveis ao terminar. Não prepara a
instalação permanente de testes combinada para o fim da Fase 3. Operações
individuais/em lote de iniciar, parar e reiniciar seguem em F3-05; recuperação
com limite de tentativas em F3-06 e backup/restauração em F3-07.

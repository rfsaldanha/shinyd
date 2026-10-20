# Estado e evidências

Este arquivo distingue implementação de homologação. O plano completo está em
[PLANO_DESENVOLVIMENTO.md](PLANO_DESENVOLVIMENTO.md).

## Marco 1 — Diagnóstico

* F0-01: plano/backlog registrados; licença MIT preservada.
* F0-02: comando somente leitura com JSON v1, códigos de saída e fixtures.
* Plataforma local observada: Ubuntu 24.04 amd64, systemd, cgroups v2.
* No momento do primeiro marco, Podman não estava instalado e sudo exigia senha.
* Não há declaração de prontidão para produção.
* Verificação: `go test -race ./...`, `go vet ./...` e build aprovados.
* `shinyd doctor --json`: requisitos observáveis presentes; alerta de Podman
  ausente. Fixtures verificam controlador ausente, permissão negada e disco
  inconclusivo; nenhum recurso foi instalado pelo diagnóstico.

## Marco 2 — Fundação administrativa e inspeção

Implementado e testado:

* Django 5.2 com migrações SQLite/WAL e grupos de prioridade iniciais.
* Login local, Argon2id, CSRF, limites de tentativas, troca/recuperação de senha,
  desativação de conta com revogação permanente de sessões anteriores.
* Cadastro/configuração de apps; permissões por usuário/grupo e revisão otimista.
* Variáveis cifradas com AES-GCM e autenticação vinculada a app/nome/revisão.
  Nenhum valor é reexibido na interface/API/auditoria.
* Upload com orçamento de bytes antes da gravação temporária; inspeção limitada,
  rejeição de traversal, links, ambiguidade, excesso de metadados e expansão.
* Fila persistente, leases, proteção contra worker antigo, idempotência,
  cancelamento e resultado com atualização periódica no navegador.
* Interface em português, assets locais e layouts desktop/móvel.

Evidências locais:

* `pytest -q`: **40 testes aprovados**.
* Ruff e migrações verificadas; `check --deploy` sem problemas, exceto a política
  deliberada de não aderir automaticamente a HSTS preload (W021 silenciado).
* Navegador Chrome real: login → cadastro → upload → worker → resultado via
  HTMX, sem recarga manual, sem erro JavaScript; viewport de 360 px sem overflow.
* Capturas de teste em `/tmp/shinyd-evidence` (não versionadas).

Escopo do marco: partes independentes de F1-01/02/04, F2-01/02/06 e F3-01.
As fases completas **não** estão concluídas. Um ZIP validado não instala pacotes,
não executa R e não cria publicação ativa. O plano administrativo não tem acesso
ao runtime; publicação, proxy Shiny, QoS efetivo, serviços instaláveis e recuperação
do produto completo ainda dependem das etapas seguintes.

As primeiras verificações de containers e sessões estão registradas nos marcos
seguintes; a integração do produto continua em desenvolvimento.

## Marco 3 — Experimentos de containers e sessões

* Podman instalado pelo operador; teste rootless inicial aprovado.
* Limitações reais: CPU não delegada e AppArmor não imposto no modo rootless.
* Primeiro teste rootful executado pelo operador: criação do container bloqueada
  por ausência da reserva de IDs `containers`; homologação ainda pendente.
* Núcleo do proxy Go com autorização antes de HTTP/WS e revalidação de WS,
  fronteiras de cabeçalhos/cookies e destinos loopback.
* Experimento com R 4.4.3/Shiny 1.10.0 e R 4.5.2/Shiny 1.13.0 simultâneos:
  afinidade, upload/download, negação sem credencial e falha isolada aprovados.
* Go: `go test -race ./...` e `go vet ./...` aprovados, incluindo revogação de WS.
* Revisão do código atual de ShinyProxy/ContainerProxy registrada, sem alegar
  execução ou benchmark da solução.

Detalhes e relatórios em [DECISOES_EXECUTOR.md](DECISOES_EXECUTOR.md).
Os experimentos não estão conectados ao painel e não habilitam execução de
código publicado. Não há declaração de conclusão do MVP ou da versão 1.0.

CI remota: marcos 1 a 4 aprovados em GitHub Actions.

## Marco 4 — Preparação de IDs para containers

* Falha real do teste rootful preservada em
  [container-rootful-prerequisito.json](evidencias/container-rootful-prerequisito.json).
* Diagnóstico antecipado da reserva de IDs, antes de baixar imagens/criar unidades.
* Preparador com prévia somente leitura e aplicação explícita: preservação das
  entradas existentes, detecção de sobreposição, locks, backups e recuperação
  após falha de escrita.
* Opção `--rootful --prepare-subids` prepara o pré-requisito e repete o teste em
  um comando. Não afrouxa isolamento e não modifica sudoers.
* Testes automatizados cobrem reserva, repetição, colisões, arquivos inseguros,
  provedor externo, disputa de locks, erro de escrita e diagnóstico antecipado.
* Verificação local: **62 testes aprovados**, Ruff e formatação aprovados; teste
  real rootless repetido com sucesso, mantendo as limitações já documentadas.

O operador aplicou a preparação e repetiu o teste; resultado abaixo.

## Marco 5 — Isolamento rootful inicial aprovado

Relatório real de 20/10/2026: [container-rootful.json](evidencias/container-rootful.json).
As **11 verificações passaram**, incluindo:

* Reserva de IDs aplicada sem alterar as faixas anteriores; repetição da prévia
  retorna `unchanged`.
* UID não-root com namespace remapeado; raiz somente leitura, tmpfs, rede
  desligada, capacidades removidas, seccomp e no-new-privileges.
* Limites de memória/processos presentes no kernel e AppArmor em modo `enforce`.
* Container sob a slice esperada, com peso de CPU 400 no ancestral.

Isso aceita o teste inicial do roteiro, não todo F0-05. Continuam pendentes
build/runtime com dados, rede de atendimento, QoS sob concorrência e reboot.
`production_ready` permanece `false`. A integração do login central com o gateway
pode avançar usando fixtures confiáveis; código enviado pelo painel continua sem execução.

## Marco 6 — Gateway com login central

* Novo binário `shinyd-gateway`, com autorização central por socket Unix privado
  e chave de serviço separada. Nenhum endpoint interno é publicado pelo painel.
* Código de acesso de uso único via POST; credencial vinculada à instância e
  à sessão Django. Banco guarda hashes, auditoria não inclui códigos/cookies.
* Autorização por rota e revalidação dos WebSockets, inclusive para apps públicos.
  Logout, revogação, falha do controle e mudança de destino negam continuidade.
* Migração aditiva de instâncias/tickets/sessões e limpeza de registros expirados.
* Headers, cookies e registro de service workers protegidos na fronteira do app.
* **85 testes Python aprovados**, testes Go com detector de condições de corrida,
  `go vet`, Ruff, migrações e verificações Django aprovados.
* Chrome real com dois Shiny: **nove cenários aprovados**, incluindo HTTP/WS
  privados, login, isolamento de cookies, replay negado, upload/download e
  revogação. Queda abrupta do autorizador encerrou o WS em 20,004 segundos.

Roteiro, decisões e limites: [GATEWAY_ACESSO.md](GATEWAY_ACESSO.md).
Evidência: [gateway-login-central.json](evidencias/gateway-login-central.json).

Esse marco implementa partes de F2-05/F5-03. Não conclui TLS, registro automático
de instâncias ou publicação: o experimento usa fixtures confiáveis e banco
descartável. Builder, agente privilegiado, rede, instalação/reboot, QoS sob
concorrência e as demais fases do plano continuam pendentes.

## Marco 7 — Fronteira do agente e pacote de diagnóstico

* `shinyd-agent` aceita apenas diagnóstico por socket Unix herdado; autentica
  o UID do chamador pelo kernel, sem identidade fornecida em cabeçalhos.
* Cliente valida o UID do listener. Não há comando arbitrário, shell, operações
  de containers, escuta TCP ou exposição de argumentos na auditoria.
* Testes Go com detector de condições de corrida aprovados. O roteiro com
  binários reais passou em **sete verificações**, incluindo reativação com o
  mesmo socket, UID indevido e argumentos recusados.
* Pacote `.deb` de desenvolvimento, unidades systemd, conta bloqueada e cliente
  administrativo. Build sem root e verificação das unidades com systemd 255.
* Roteiro privilegiado aprovado no Ubuntu 24.04 descartável do CI: instalação,
  identidade autorizada/negada, limites e restrições observados no kernel,
  reinício do agente e recriação do socket. Remoção preservou arquivo de teste
  em `/var/lib/shinyd`. Esse teste de CI não alterou o host de desenvolvimento.

Contrato, instalação e limites: [AGENTE_EXECUCAO.md](AGENTE_EXECUCAO.md).
Evidência sem privilégios: [agente-diagnostico.json](evidencias/agente-diagnostico.json).
Evidência do CI: [agente-ci.json](evidencias/agente-ci.json) e
[execução com pacote e relatório](https://github.com/rfsaldanha/shinyd/actions/runs/39430925787).
É a primeira parte de F1-03/F1-05; criação/parada de containers, rede, instalação
do controle completo, registro no gateway e reboot continuam pendentes.

## Marco 8 — Diagnóstico do agente no painel

* Página exclusiva de administradores consulta o agente por socket autenticado.
  Publicadores/usuários não acionam a consulta nem veem o diagnóstico.
* Mostra pré-requisitos e data da observação; rejeita respostas incompatíveis,
  excessivas, desatualizadas ou de outro UID. Sem redirecionamento/TCP alternativo.
* Ausência do agente tem mensagem própria e não impede acesso ao catálogo.
* **106 testes Python aprovados**, incluindo 21 novos testes do cliente,
  autorização da página, dados malformados, indisponibilidade e escape HTML.
* Chrome real com agente e banco descartáveis: diagnóstico, disposição em
  360 px e queda do serviço aprovados. O fluxo anterior de login, cadastro,
  upload, worker e atualização HTMX continuou aprovado.

Esta é uma parte de F1-06. Não substitui observação de instâncias, métricas de
apps nem a instalação do painel como serviço. Próximo marco: operações tipadas
de containers e isolamento da rede, antes da conexão com o fluxo de publicação.

## Marco 9 — Agente instalado e homologado localmente

O operador instalou `shinyd-agent 0.1.0~agent1` e executou o roteiro privilegiado
em 20/10/2026. As **15 verificações passaram**, incluindo:

* Checksum e identidade do pacote; conta bloqueada e socket privado.
* Consulta pela identidade autorizada e recusa de root como chamador implícito.
* Capacidades removidas, no-new-privileges e seccomp observados no processo.
* Namespace de rede separado e limites de memória/processos no cgroup de controle.
* Recuperação após reiniciar o agente e recriar o socket, com auditoria.

A conferência posterior confirmou serviço/socket ativos, socket habilitado,
permissão `0660` e dono `root:shinyd-control`. O checksum do pacote local confere
com o relatório. Evidência: [agente-servico-local.json](evidencias/agente-servico-local.json),
sem o identificador do boot do host; o original permanece em `var/`.

Isso conclui a homologação local da capacidade de diagnóstico. Não comprova
recuperação após reboot, instalação do painel/worker/gateway ou execução de
containers pelo agente. O painel de desenvolvimento continua sob o usuário do
desenvolvedor e não ganha autorização para o socket de sistema.

## Marco 10 — Executor tipado e transporte por socket Unix

Implementados `shinyd-runtime` e cliente, com catálogo root de imagens locais
imutáveis, estado durável, operações criar/consultar/parar/remover, idempotência,
proteção de recursos alheios e admissão conservadora. O pacote é separado do
diagnóstico e instala execução desabilitada.

O caminho de atendimento usa containers sem rede externa e relay Unix para
o namespace próprio da instância. Gateway e autorizador aceitam somente o
socket canônico vinculado ao UUID. A API não aceita comandos, imagens, volumes
ou opções de Podman fornecidos pelo chamador.

Validação local: **107 testes Python**, testes Go com detector de concorrência,
`go vet`, Ruff e verificação das unidades systemd aprovados. O roteiro privilegiado
passou em **25 verificações** no Ubuntu 24.04 descartável do
[CI](https://github.com/rfsaldanha/shinyd/actions/runs/39440034585):

* Dois Shiny para R 4.4.3, atendidos por HTTP/WebSocket no navegador real.
* AppArmor imposto, inclusive quando combinado ao perfil do `crun`.
* Sem acesso à rede do host, metadados externos ou sockets de controle.
* Upload/download, estado reativo e parada de uma instância preservando a sessão da outra.
* Idempotência, recusa de argumentos arbitrários/UID indevido e admissão limitada.
* Reinício do executor, reconexão a containers existentes e nova sessão no navegador.
* Partida malsucedida com limite de tentativas, preservando o outro app.
* Orçamento de memória agregado e ausência de saída dos containers na auditoria.

As **13 verificações posteriores do agente de diagnóstico** também passaram.
Evidência: [executor-ci.json](evidencias/executor-ci.json), incluindo versões,
hardware e perfil de teste. Isso comprova este cenário funcional, sem medir
capacidade de usuários, pressão de memória ou QoS sob concorrência.

Contrato e limites: [EXECUTOR_CONTAINERS.md](EXECUTOR_CONTAINERS.md).
Uploads continuam sem execução; faltam builder, registro automático no painel,
drenagem, dados persistentes e QoS sob carga. Reinício do executor encerra as
conexões dos relays, embora preserve containers já em execução.

## Marco 11 — Executor homologado localmente

O operador instalou `shinyd-runtime 0.1.0~runtime1` e executou o roteiro
privilegiado em 20/10/2026. As **25 verificações passaram** no host local,
incluindo dois apps Shiny reais, isolamento, admissão, parada de uma instância
sem interromper a sessão da outra e partida malsucedida com tentativas limitadas.
Na recuperação após reiniciar o executor, o roteiro conferiu tanto a identidade
do container quanto o PID do processo no host; a sessão do navegador foi recriada.

Evidência: [executor-local.json](evidencias/executor-local.json), cópia do
relatório de `var/runtime-report.json`. O ambiente observado tinha kernel
`7.0.0-38-generic`, 16 CPUs lógicas, 66.735.202.304 bytes de RAM, Podman 4.9.3
e cgroups v2. Os apps usaram R 4.4.3 e Shiny 1.10.0, com AppArmor em modo
`enforce`. Este cenário não mede capacidade de atendimento.

A conferência posterior confirmou:

* Checksum SHA-256 do pacote local igual ao registrado no relatório:
  `4f6cfbe5b265327a405179ce2063f4bbc1d5a667b934e6c1463f5f1f6588dd29`.
* Pacote instalado; serviço e socket do executor inativos, com resultado
  `success`, e socket desabilitado, como previsto na finalização do roteiro.
* Serviço e socket do agente de diagnóstico ativos, com socket habilitado.

Isso conclui a homologação local deste executor. Publicação de ZIPs, builder,
TLS, dados persistentes, drenagem, recuperação após reboot e QoS sob carga
continuam pendentes. O próximo marco é o builder isolado (F2-03), preparando
artefatos verificados para conectar o fluxo de publicação ao executor.

## Marco 12 — Builder offline de ZIPs validado no CI

Implementados pacote `shinyd-builder` e comando administrativo `shinyd-build`.
O fluxo valida novamente o ZIP, resolve R/pacotes contra catálogo imutável,
prepara biblioteca renv privada em container sem rede e sela imagem OCI para o
executor. Tem exclusão mútua, intenção durável, limpeza por identidade, cache
por app/código/ambiente e erros sem saída de código publicado.

Os **141 testes Python**, verificações Go, Ruff e verificações Django passaram.
O helper R restaurou uma biblioteca com 40 pacotes em fixture local sem rede;
este experimento rootless valida o helper, não o isolamento rootful instalado.

As **25 verificações do pacote e fluxo real** passaram no
[CI Ubuntu 24.04](https://github.com/rfsaldanha/shinyd/actions/runs/39449102048):

* R 4.4.3/Shiny 1.10.0 e R 4.5.2/Shiny 1.13.0 preparados a partir de ZIPs e locks.
* AppArmor imposto, identidades remapeadas, rede isolada e biblioteca privada.
* Cache repetido preservando o ID da imagem; recusa de checksum diferente.
* Duas imagens iniciadas pelo executor, com reatividade, upload e download em Chrome.
* Concorrência recusada, operador sem privilégio recusado e recuperação de um
  container/registro interrompido simulado, sem reiniciar o host.
* CPUWeight=50 e memória agregada de 1 GiB no grupo de manutenção.
* ZIP inseguro, R incompatível, pacote ausente e restauração inválida recusados,
  preservando os artefatos anteriores e os PIDs dos dois processos R existentes.

Evidência: [builder-ci.json](evidencias/builder-ci.json), com commit, kernel,
CPU/RAM, Podman, imagens e inventários. Os cinco jobs do CI passaram, incluindo
as verificações anteriores do executor e agente. Não é medição de capacidade.

A revisão também corrigiu o limite inicial de `shinyd-workloads.slice`:
`MemoryMax=0` era ignorado pelo parser do systemd. O pacote agora usa o mínimo
positivo `1` até o executor aplicar o orçamento real; o builder recusa orçamento
não configurado. Isso não habilita execução automaticamente.

Uma execução inicial no CI, com kernel `6.17.0-1022-azure`, retornou
`runtime_unavailable` ao iniciar o primeiro artefato. Foram acrescentados
diagnósticos para as fixtures. As duas execuções seguintes passaram com kernel
`7.0.0-1017-azure`; a causa da primeira falha não foi confirmada. Partidas frias
e a matriz de kernels precisam de nova verificação antes do MVP operacional.

Este é um recorte de F2-03: exige lockfile e pacotes já disponíveis na base;
não instala dependências externas nem conecta o builder ao painel. A API tipada
de build, integração com worker e ativação de candidatas continuam pendentes.
Contrato e demonstração: [BUILDER_OFFLINE.md](BUILDER_OFFLINE.md).

## Marco 13 — Builder homologado localmente

O operador instalou `shinyd-builder 0.1.0~builder1` e executou o roteiro
privilegiado em 20/10/2026. As **25 verificações passaram** no host local.
Evidência: [builder-local.json](evidencias/builder-local.json), cópia integral
do relatório de `var/builder-report.json`.

O ambiente observado tinha kernel `7.0.0-38-generic`, 16 CPUs lógicas,
66.735.202.304 bytes de RAM e Podman 4.9.3. Os dois artefatos usaram R 4.4.3
com Shiny 1.10.0 e R 4.5.2 com Shiny 1.13.0, cada um com biblioteca privada
de 40 pacotes. Foram conferidos:

* AppArmor em modo `enforce`, identidades remapeadas e rede isolada.
* Cache dos artefatos, execução simultânea e reatividade, upload e download
  no navegador.
* Recusa de builds concorrentes, checksum incorreto e operador sem privilégio.
* Peso de CPU 50 e limite agregado de memória de 1 GiB na manutenção.
* Recuperação de registro/container interrompido simulado; falhas de ZIP,
  versão de R, pacote e restauração preservando artefatos e processos R.
* Ausência de build pendente ao final.

A conferência posterior confirmou o SHA-256 do pacote local igual ao relatório:
`d54494cea778f86b57d282bb76141f0988fad2581835ed78df87d065c6106a6c`.
O serviço e socket do executor estavam inativos, com resultado `success`, e
o socket desabilitado, conforme a finalização do roteiro. O agente de diagnóstico
continuou ativo, com socket habilitado. O builder é um comando administrativo,
sem serviço persistente.

Este resultado conclui a homologação local do builder offline. Não esclarece
a falha anterior no kernel `6.17.0-1022-azure`, nem homologa recuperação após
reboot, capacidade, QoS sob carga ou quotas de disco. O próximo marco é a API
privada tipada e sua integração com o worker do painel, incluindo progresso,
cancelamento e ativação da candidata preservando a publicação anterior.
Downloads de dependências externas continuam fora deste contrato.

## Marco 14 — Preparação de versões integrada ao painel

Implementados API privada de build, cliente autenticado por UID, integração com
o worker e botão **Preparar versão**. O publicador seleciona um ambiente do
catálogo e acompanha fases, falhas e cancelamento. O ZIP é transmitido como
bytes; o painel não ganha sudo nem fornece caminhos, imagens ou comandos.

O pacote `shinyd-builder 0.1.0~builder2` separa a API na slice de controle e os
trabalhos em unidades systemd de manutenção. Registros duráveis e idempotência
permitem retomar consultas após perder a resposta ou reiniciar a API/worker.
Um cancelamento anterior ao envio impede submissão tardia. Falha de limpeza
permanece visível e não é apresentada como cancelamento concluído.

Os **168 testes Python** passaram, incluindo transporte Unix real com recusa de
UID indevido, requisições inválidas, concorrência, retomada, upload interrompido,
cancelamento, conflito de leases e revogação de permissão. Ruff e verificações
Django/migrações passaram. O pacote foi gerado e as novas unidades foram
verificadas pelo systemd; a verificação também apontou o `MemoryMax=0` do pacote
antigo do executor ainda instalado neste host, já corrigido no código no marco 12.

As **33 verificações do roteiro privilegiado** passaram no
[CI](https://github.com/rfsaldanha/shinyd/actions/runs/39463341316), incluindo
views e worker Django contra a API instalada, com a identidade real
`shinyd-control` e banco descartável. Os cinco jobs passaram. Evidência:
[builder-panel-ci.json](evidencias/builder-panel-ci.json). O runner usou kernel
`6.17.0-1022-azure`, 4 CPUs lógicas, 16.766.414.848 bytes de RAM e Podman 4.9.3.

O primeiro ensaio da integração falhou no roteiro: o estado do painel tinha sido
colocado sob o diretório do agente, `root:root 0750`, inacessível à conta de
controle. A demonstração agora usa diretório e ambiente Python descartáveis
próprios, sem ampliar permissões de diretórios existentes. O banco local de
desenvolvimento recebeu a migração após backup privado em `var/backups/`.

A revisão também separou as chaves de idempotência de upload e preparação e
preservou o erro `busy` quando um builder CLI já ocupa o lock, sem atribuir a
esse trabalho uma limpeza pertencente ao outro.

A homologação local do marco 13 refere-se ao contrato CLI do pacote anterior.
As falhas intermitentes anteriores de cache,
partida e limpeza continuam sem causa confirmada; este sucesso no kernel 6.17
não comprova resolução dessas falhas nem capacidade sob carga.

Na rodada seguinte, as 33 verificações passaram, mas a limpeza de duas instâncias
falhou; [registro](evidencias/builder-panel-cleanup-ci.json). O roteiro agora
respeita HTTP 429/Retry-After, com até quatro tentativas sobre o mesmo UUID,
e registra as repetições. Outros erros continuam encerrando a operação; testes
negativos observam a primeira recusa. Esse ajuste não altera o executor nem
comprova a causa da falha anterior, cujo código HTTP não foi registrado.
A rodada posterior passou integralmente, incluindo a limpeza, sem receber 429;
o relatório registra `runtime_api_retries: []`. Os testes unitários cobrem a
repetição limitada e a preservação de falhas diferentes de 429.

Este marco cobre a integração de F2-03 e parte de F2-06. **Ainda não ativa
publicações**: o próximo contrato deve permitir ao executor reconhecer um
artefato preparado, iniciar a candidata e registrar sua rota após readiness,
preservando a publicação anterior em caso de falha. Downloads, retenção visual,
prioridades da fila e homologação de reboot continuam pendentes.
Contrato, limites e demonstração: [BUILDER_PAINEL.md](BUILDER_PAINEL.md).

### Tentativa local de builder2 e correção da preparação Python

Em 20/10/2026, o operador executou o pacote `0.1.0~builder2`. O
[relatório local](evidencias/builder-panel-local-setup.json) registra **26
verificações aprovadas**, seguidas de falha antes dos testes de integração com
o painel. Não há erro de limpeza no relatório. O SHA-256 conferido do pacote foi
`c301c391636c27759072cf984a7c7b2838fb73bc69080dbe8e2f313f5a333dd9`.
O host usou kernel `7.0.0-38-generic`, 16 CPUs lógicas,
66.735.198.208 bytes de RAM e Podman 4.9.3.

A reprodução com o mesmo Python 3.12.3 confirmou que `python -m venv` falha
porque `ensurepip` está ausente. O roteiro agora cria o ambiente descartável
sem pip e usa o pip do interpretador invocador para instalar as dependências
fixadas nesse ambiente. A preparação ocorre antes dos builds e identifica sua
etapa no relatório, sem expor saídas que possam conter credenciais.

A preparação corrigida passou neste host, sem sudo, incluindo todas as
dependências de `requirements.lock` e importações isoladas de Django 5.2.18,
Argon2 e cryptography. Essa verificação confirmou a correção da preparação
Python. Naquela rodada, a homologação local completa ainda aguardava a
repetição privilegiada pelo operador, registrada no marco seguinte.

O commit `26620d6` passou nos cinco jobs do
[CI](https://github.com/rfsaldanha/shinyd/actions/runs/39465438999).
O roteiro corrigido completou as **33 verificações**, sem erro de limpeza nem
repetição da API do executor. A preparação Python registrou
`panel_setup: {"stage": "dependencies", "passed": true}`.
Evidência: [builder-panel-bootstrap-ci.json](evidencias/builder-panel-bootstrap-ci.json).
Localmente, os 168 testes Python e as verificações Ruff também passaram.

## Marco 15 — Preparação pelo painel homologada localmente

O operador repetiu o roteiro corrigido em 20/10/2026. As **33 verificações
passaram**, com `passed: true`, sem erro de limpeza e sem repetição da API do
executor. Evidência: [builder-panel-local.json](evidencias/builder-panel-local.json),
cópia integral de `var/builder-panel-report.json`, iniciado às 17:28:36 UTC.

O pacote instalado é `shinyd-builder 0.1.0~builder2`. O SHA-256 conferido do
arquivo local coincide com o relatório:
`c301c391636c27759072cf984a7c7b2838fb73bc69080dbe8e2f313f5a333dd9`.
O host usou kernel `7.0.0-38-generic`, 16 CPUs lógicas,
66.735.198.208 bytes de RAM e Podman 4.9.3. Os dois ambientes R foram
4.4.3/Shiny 1.10.0 e 4.5.2/Shiny 1.13.0.

Além dos cenários de build, isolamento, cache e execução simultânea dos apps,
o roteiro confirmou, com a identidade real `shinyd-control` e banco descartável:

* Upload e preparação pelas views Django, com criação de artefato novo.
* Retomada pelo worker após reiniciar a API de build.
* Cancelamento confirmado e negação de acesso a versões/trabalhos de outro usuário.
* Ausência do segredo de teste no manifesto e na página consultada.

A consulta posterior ao systemd confirmou builder e executor inativos com
resultado `success`, e seus sockets desabilitados. O socket do agente de
diagnóstico permaneceu habilitado e escutando; seu serviço estava inativo,
disponível para ativação pelo socket.

Isso conclui a homologação local da preparação de versões integrada ao painel.
O roteiro usa cliente HTTP de testes Django para as telas e Chrome para os
apps R; não é um teste de navegação visual da nova tela de preparação.
O próximo marco é o contrato de ativação: reconhecer artefato preparado,
iniciar a candidata, verificar funcionamento e registrar a rota no gateway,
preservando a publicação anterior em caso de falha. Ativação, reboot, capacidade,
QoS sob carga, downloads externos e os demais itens do plano continuam pendentes.

## Marco 16 — Ativação de versões pelo painel

Implementada ativação explícita de versões preparadas, com confirmação do
impacto sobre sessões. O trabalho cria uma candidata com UUID persistente,
consulta/solicita a partida e confere o resultado antes de trocar publicação
e rota na mesma transação. A candidata que falha não substitui a anterior.

O pacote `shinyd-runtime 0.1.0~runtime2` aceita chaves de artefatos somente
quando a política habilita `builder_artifacts`. Confere manifesto protegido
do builder, app, checksum do código, digest e rótulos da imagem local.
O controle não fornece imagens, caminhos ou comandos. A partida mantém o
isolamento e as verificações de funcionamento do executor existente.

Cancelamento e limpeza usam encerramento definitivo persistente, inclusive
antes da primeira partida. Um pedido atrasado não recria a instância removida.
Tentativas sobre artefatos não relançam automaticamente processos que falharam.
Perda de resposta e retomada do worker usam o mesmo UUID. Mudança de permissão,
configuração ou cancelamento durante a partida impede a troca.

Após a troca, a rota anterior é desabilitada e seu container é removido.
Limpeza pendente permanece visível e é retomada; não bloqueia o atendimento
da versão já ativada. Uma nova ativação do mesmo app aguarda essa limpeza.
O botão **Abrir aplicativo** usa o login central e a autorização existentes.
Não há drenagem: a interface informa que a troca interrompe sessões antigas.

Os **192 testes Python** passaram localmente, incluindo respostas inválidas do
executor, imagem divergente, domínio inválido sem candidata residual, perda de
resposta, cancelamento, expiração, configuração/conta alterada durante a partida,
limpeza pendente e impedimento de conclusão por worker com lease antigo.
Go passou em `go test -race ./...` e `go vet ./...`, incluindo manifestos
inseguros, artefato de outro app e encerramento definitivo após recuperação.
Ruff, migrações e verificações Django passaram.

A migração `0006` foi aplicada ao banco local após backup privado em
`var/backups/metadata-before-activation-20261020T175633Z.sqlite3`.
O pacote `runtime2` foi gerado e suas unidades verificadas pelo systemd.
Naquela rodada, a instalação e a homologação desse pacote no host local
dependiam do operador, pois `sudo` exigia senha. O resultado da execução local
está registrado no marco seguinte.

O primeiro [CI](https://github.com/rfsaldanha/shinyd/actions/runs/39470308455),
no commit `94787e3`, passou nos cinco jobs e nas **49 verificações** do roteiro
ampliado. Incluiu partida real, negação de artefato alheio, autorização pública
e restrita, falha de inicialização preservando a publicação anterior,
cancelamento, recusa de partida atrasada, troca e retirada da instância antiga.
Chrome confirmou reatividade, upload e download da instância ativada por meio
do proxy de teste e do transporte Unix do executor.

A rodada do commit `6144f72` também passou nos cinco jobs e nas 49 verificações:
[CI](https://github.com/rfsaldanha/shinyd/actions/runs/39471110057) e
[relatório integral](evidencias/activation-ci.json). O runner usou kernel
`7.0.0-1017-azure`, 4 CPUs lógicas, 16.760.344.576 bytes de RAM e Podman 4.9.3.
Não houve erro de limpeza nem repetição por executor ocupado nessa execução.
O roteiro limita a quatro tentativas as verificações de recusa que recebem
`runtime_busy`, sempre sobre o mesmo identificador, e registra as repetições.

A revisão final atualizou os textos do catálogo, upload e configurações,
identificou o botão de confirmação como **Ativar versão** e preservou quebra
de linha do digest em telas pequenas. Os 41 testes pertinentes de ativação,
acesso e segredos passaram após esses ajustes. O smoke local no Chrome passou
por login, cadastro, upload, worker, atualização da página e viewport de 360 px;
isso não substitui o ensaio completo de ativação no navegador.

Limites: não injeta variáveis/segredos (apps com valores configurados têm
ativação bloqueada), não oferece volumes persistentes nem rede externa,
entrada estável por app ou TLS instalado. Estado contínuo no painel e drenagem
continuam pendentes. Marcas de encerramento contam no limite de 256 registros
do executor; coleta segura exige trabalho posterior. Não há medição de
capacidade, nem homologação de reboot ou do fluxo visual completo de
painel/gateway/TLS neste ensaio. O MVP e a fase 2 completa não estão concluídos.
Contrato, configuração e comando de homologação: [ATIVACAO_PAINEL.md](ATIVACAO_PAINEL.md).

## Marco 17 — Ativação pelo painel homologada localmente

O operador executou o roteiro com `--activation` e instalação dos dois pacotes
em 20/10/2026. As **51 verificações passaram**, com `passed: true`, sem erro de
limpeza e sem repetição por executor ocupado. Evidência:
[activation-local.json](evidencias/activation-local.json), cópia integral de
`var/activation-report.json`, iniciado às 18:17:44 UTC.
São as 49 verificações do CI mais a conferência do checksum e da identidade
do pacote local do executor.

O inventário instalado e os SHA-256 dos arquivos locais foram conferidos com
o relatório:

| Pacote instalado | SHA-256 do `.deb` |
|---|---|
| `shinyd-runtime 0.1.0~runtime2` | `d1e9f9b7302036e5a8d1dcb8e52a25037fe79bc3eb5122d3f047be47706f3da6` |
| `shinyd-builder 0.1.0~builder2` | `c301c391636c27759072cf984a7c7b2838fb73bc69080dbe8e2f313f5a333dd9` |

O host usou kernel `7.0.0-38-generic`, 16 CPUs lógicas,
66.735.198.208 bytes de RAM e Podman 4.9.3. Os ambientes R
4.4.3/Shiny 1.10.0 e 4.5.2/Shiny 1.13.0 funcionaram simultaneamente.
A preparação Python descartável também terminou sem erro.

Além dos cenários de preparação, o ensaio confirmou:

* Ativação pelo worker e autorização de rotas públicas/restritas.
* Recusa de artefato alheio e de ativação com segredos ainda não aplicáveis.
* Falha de inicialização preservando a publicação anterior, com mensagem segura.
* Cancelamento confirmado e recusa de partida atrasada.
* Troca de publicação, retirada da instância anterior e revogação de seu host.
* Reatividade, upload e download da instância ativada no Chrome.

A consulta posterior ao systemd confirmou builder e executor inativos, ambos
com resultado `success`, e seus sockets desabilitados. O socket do agente de
diagnóstico permaneceu habilitado e escutando; seu serviço estava inativo.
Esse é o estado esperado após a limpeza da demonstração, não uma instalação
completa do produto em atendimento.

As operações do painel foram exercitadas pelo cliente HTTP Django, enquanto
o navegador acessou os apps pelo proxy de teste. O fluxo visual completo
painel/gateway/HTTPS ainda não foi homologado. Este ensaio não mede capacidade,
QoS sob carga ou recuperação após reboot. Permanecem os limites do marco 16.

O próximo marco prioritário é integrar os serviços de painel, worker,
autorização e gateway em uma instalação reproduzível, com HTTPS e demonstração
completa no navegador (partes de F1-03/F2-05). O aceite deve percorrer upload,
preparação, ativação e acesso público/restrito, incluindo recusa por URL direta
e WebSocket. Isso precede a declaração de conclusão da fase 2; injeção de
segredos, volumes persistentes e demais itens da fase 3 continuam no backlog.

## Marco 18 — Pacote integrado e roteiro HTTPS

Implementado o pacote experimental `shinyd-control 0.1.0~control1` para Ubuntu
24.04 amd64/Python 3.12. Inclui dependências Python fixadas, assets locais,
gateway Go, unidades systemd, configuração inicial e comando administrativo.
Painel e autorizador usam Gunicorn; Caddy recebe HTTPS com certificado fornecido
pelo operador. O gateway instalado recebe socket Unix herdado do systemd.

A instalação mantém o painel parado até configurar origens, certificado,
políticas, banco e conta administrativa. Configuração repetida preserva chaves
e cria backup privado de metadados antes das migrações. A remoção preserva dados.
Painel, worker e autorizador usam a conta de controle; gateway e entrada HTTPS
possuem identidades próprias. Sockets privados substituem listeners TCP internos.

Os 217 testes Python passaram localmente, incluindo rejeição de configurações ambíguas,
injeção em nomes de host, certificado sem cobertura, chave divergente e porta
HTTPS na ativação e preservação do socket herdado durante dois reinícios reais
do Gunicorn. Go passou em `go test -race ./...` e `go vet ./...`.
Ruff, migrações e `check --deploy` passaram. O pacote local foi gerado.

O roteiro ampliado de homologação usa os serviços instalados e Chrome para
cadastro, upload, preparação, ativação e uso de Shiny por HTTPS. Inclui acesso
público/restrito, recusa de URL/WebSocket, cookies, transferências, reinício de
painel/worker sem encerrar Shiny e limpeza. A execução integrada passou nas **86 verificações** do
[CI](https://github.com/rfsaldanha/shinyd/actions/runs/39478265938), no commit
`11f41faf9f75f2bacd91248605840e5c87f1a9df`. Os cinco jobs passaram, incluindo
a verificação separada de remoção do pacote preservando um arquivo de estado.
Evidência integral: [installed-https-ci.json](evidencias/installed-https-ci.json).

O runner usou kernel `7.0.0-1017-azure`, 4 CPUs lógicas,
16.760.336.384 bytes de RAM e Podman 4.9.3. Não houve erro de limpeza nem repetição
da API do executor. O pacote de controle testado teve SHA-256
`f53342a32df85ede1634933a2e478acb76584ef2789a9fd5cbbb9909c1730b2a`.
O navegador usou HTTPS com certificado de teste fixado por chave pública;
o socket TLS também foi verificado com confiança explícita e conferência de hostname.

O ensaio inicial chegou à publicação e ao controle de acesso, mas perdeu a sessão
após reiniciar painel/worker. O [relatório inicial](evidencias/installed-initial-ci.json)
registra essa falha. A dependência `Requires` do target propagava o reinício
à pilha; foi substituída por `Wants`, mantendo `PartOf` nos componentes.
No ensaio aprovado, os PIDs de autorizador, gateway e entrada HTTPS ficaram
idênticos antes/depois do reinício, e o Shiny manteve processo e estado da sessão.
Gunicorn também preserva o socket herdado do systemd, com regressão local real.

A revisão final acrescentou quatro testes de recusa de instalação preexistente
ou parcial: dados, configuração, certificado e link pendente são preservados.
Esses testes compõem o total local de 217; o CI citado executou os 213 testes
anteriores e o mesmo código dos serviços instalados.

Naquela rodada, a homologação no host local dependia da execução privilegiada
pelo operador, pois `sudo` solicitava senha. O resultado está no marco seguinte.

Guia e comando: [INSTALACAO_INTEGRADA.md](INSTALACAO_INTEGRADA.md).
Emissão/renovação automática de certificados, assistente de catálogo, instalação
em um comando, reboot real, recuperação de atualização e demais itens do MVP
continuam pendentes. A configuração exige políticas aprovadas e certificado
fornecido, e recusa mudanças de domínio de uma instalação existente.

## Marco 19 — Instalação integrada homologada localmente

O operador executou o roteiro integrado em 20/10/2026. As **88 verificações
passaram**, com `passed: true` e sem erro de limpeza. Evidência:
[installed-https-local.json](evidencias/installed-https-local.json), cópia integral
de `var/installed-report.json`, iniciado às 19:05:18 UTC.
São as 86 verificações do CI mais checksum e identidade do pacote local do executor.

Os três pacotes instalados e os hashes dos arquivos locais foram conferidos
com o relatório:

| Pacote instalado | SHA-256 do `.deb` |
|---|---|
| `shinyd-control 0.1.0~control1` | `0c2223b57c3099c647b40c58fd9b871c0bfd7e60563244f1068d4a8c276fe8db` |
| `shinyd-runtime 0.1.0~runtime2` | `d1e9f9b7302036e5a8d1dcb8e52a25037fe79bc3eb5122d3f047be47706f3da6` |
| `shinyd-builder 0.1.0~builder2` | `c301c391636c27759072cf984a7c7b2838fb73bc69080dbe8e2f313f5a333dd9` |

O host usou kernel `7.0.0-38-generic`, 16 CPUs lógicas,
66.735.198.208 bytes de RAM e Podman 4.9.3. A consulta ao inventário instalado
confirmou Caddy `2.6.2-6ubuntu0.24.04.3`. Os ambientes R
4.4.3/Shiny 1.10.0 e 4.5.2/Shiny 1.13.0 funcionaram simultaneamente.

O navegador percorreu login, cadastro, envio de ZIP, inspeção, preparação,
ativação e uso de Shiny pelos serviços instalados e HTTPS. Foram confirmados
cookies seguros, bloqueio de URL direta e WebSocket privados, acesso público,
reatividade, afinidade, upload/download e apresentação do painel em 360 px.
A configuração preservou as chaves existentes e criou backup privado de
metadados; uma tentativa de configurar serviços ativos foi recusada.

O reinício do painel e do worker preservou o processo e o estado da sessão
Shiny. Os PIDs do autorizador (`210195`), gateway (`210196`) e entrada HTTPS
(`210197`) permaneceram iguais antes/depois. A remoção de uma fixture recebeu
uma resposta HTTP 429 do executor; a repetição limitada prevista no roteiro
concluiu a operação. Não houve repetição nas verificações negativas de ativação.

A consulta posterior ao systemd confirmou painel, autorizador, worker, gateway,
entrada HTTPS, manutenção, builder e executor inativos, com resultado `success`.
Seus sockets estavam inativos e `shinyd.target` desabilitado. O socket do agente
de diagnóstico continuou habilitado e escutando. Os pacotes permanecem instalados;
o ensaio descartável não deixa o painel configurado para atendimento permanente.

Isso conclui a homologação local deste marco. O teste usa certificado descartável
e nomes `.test`; não comprova DNS público, emissão/renovação automática, reboot,
capacidade ou QoS sob carga. A remoção do pacote preservando um arquivo de estado
foi verificada separadamente no CI, não repetida nesta execução local.

O próximo marco de implementação é F3-01: aplicar variáveis e segredos ao
processo isolado, com autorização e revisão de configuração, sem reenviar o ZIP
e informando a necessidade de reinício. O armazenamento cifrado existente será
preservado; hoje a ativação de apps com valores configurados continua bloqueada.
Volumes persistentes, rollback com dados, drenagem e demais itens do MVP seguem
no backlog.


## Marco 20 — Configuração cifrada aplicada ao runtime (F3-01)

Implementados injeção de variáveis/segredos na partida, cópia cifrada por
publicação, remoção e aplicação explícita sem upload/rebuild. O painel distingue
revisão desejada e aplicada e exige confirmação da interrupção de sessões.
Contratos antigos sem configuração permanecem compatíveis. Pacotes novos:
`runtime3` (crun explícito e descritor de memória) e `control2` (migração 0007).
O builder permanece na versão 2.

Passaram localmente 235 testes Python, testes Go com detector de concorrência,
`go vet`, Ruff, verificação de migrações e diagnóstico Django de produção. A
migração 0007 foi aplicada ao banco de desenvolvimento depois do backup privado
`var/backups/metadata-before-environment-20261020T193521Z.sqlite3`.

O primeiro [ensaio de CI](https://github.com/rfsaldanha/shinyd/actions/runs/39484654440)
comprovou partida com configuração cifrada, valores recebidos pelo R (inclusive
UTF-8 e quebras de linha), rotação sem ZIP e preservação da sessão durante reinício
do painel/worker. O roteiro parou na asserção seguinte: exigia 403 para a rota
anterior, embora o contrato do gateway retorne 404 para publicações desativadas.
O relatório integral com `passed: false` está em
[secrets-initial-ci.json](evidencias/secrets-initial-ci.json). A correção do roteiro
em `366eaaa` exige 404 e registra o status HTTP observado.

A [execução final](https://github.com/rfsaldanha/shinyd/actions/runs/39486005117),
no commit `366eaaaddd71d1b19e41ecf7f662e6823d142165`, passou em **todos os cinco jobs**.
O ensaio integrado passou nas **99 verificações**, sem erro de limpeza nem
repetições por executor ocupado. O relatório completo está em
[secrets-runtime-ci.json](evidencias/secrets-runtime-ci.json), extraído do job
`118846469452`. A rota anterior retornou HTTP 404, como esperado.

O navegador confirmou a entrega de valores ao R, rotação sem reenviar ZIP
(permanece uma única versão de código), remoção aplicada na nova instância,
revisão antiga mantida até a confirmação, acesso privado/público por HTTP e
WebSocket e manutenção dos demais processos. Os valores sintéticos não apareceram
em metadados/logs dos containers, logs dos serviços ou banco SQLite em texto claro.
As outras aplicações continuaram reativas e sem a variável do app de teste.
Os PIDs de auth/gateway/ingress permaneceram `11447`/`11448`/`11449` durante o
reinício de painel/worker; o processo e a sessão Shiny foram preservados.

Ambiente do ensaio: Ubuntu 24.04 do workflow, kernel `7.0.0-1017-azure`,
4 CPUs lógicas, 16.760.344.576 bytes de RAM e Podman 4.9.3. Isso é validação
funcional, sem medição de capacidade, reboot ou disputa de QoS.
O job também removeu o pacote do controle e verificou a preservação do estado.

A homologação no host local ainda está pendente: o sudo continua exigindo senha.
Os pacotes foram construídos e as unidades conferidas, sem alterar os serviços
instalados. O ensaio local das versões anteriores não comprova a nova injeção.
Os arquivos disponíveis neste host têm estes hashes (o CI identifica seus
próprios pacotes no relatório):

| Pacote local | SHA-256 do `.deb` |
|---|---|
| `shinyd-runtime 0.1.0~runtime3` | `30f2dfaf50ea57ad2e6b84de986e0cabbb0283269bb8a3f3e40f5096268771b5` |
| `shinyd-control 0.1.0~control2` | `d3c59d34dce5c035f903047931dcaf3490eea2bf7bc01738cd15684970440b81` |

Comando local: [VARIAVEIS_SEGREDOS.md](VARIAVEIS_SEGREDOS.md), com relatório em
`var/secrets-report.json`. O roteiro preserva instalações existentes recusando
prosseguir quando encontra configuração ou dados de controle.

Contrato, operação, limites e fontes: [VARIAVEIS_SEGREDOS.md](VARIAVEIS_SEGREDOS.md).
Persistência dos dados dos apps (F3-02), rollback, drenagem e recuperação após
reboot seguem no backlog; esta entrega não declara a fase 3 inteira concluída.

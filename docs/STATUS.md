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

Na primeira tentativa local de 21/10/2026, a instalação parou por falta de
`crun` (diagnóstico e recuperação no marco 21). A repetição após instalar a
dependência passou; a homologação local da nova injeção está no marco 22.
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

## Marco 21 — Dependência ausente na homologação local de F3-01

Em 21/10/2026, a execução do operador gerou `var/secrets-report.json` às
11:25:23 UTC, com `passed: false`. As três verificações iniciais passaram
(instalação de teste vazia, checksum e identidade do pacote), mas `dpkg --install`
falhou antes de preparar ambientes ou iniciar apps. A cópia integral está em
[secrets-dependency-local.json](evidencias/secrets-dependency-local.json).

O diagnóstico local confirmou `crun` ausente, inclusive `/usr/bin/crun`;
`shinyd-runtime 0.1.0~runtime3` estava desempacotado, sem configuração (`iU`).
Podman 4.9.3, builder2 e control1 continuavam instalados. O log de dpkg registra
a troca de runtime2 para runtime3 às 08:25:23 no fuso local, encerrando no estado
`unpacked`. Os serviços runtime, builder e painel estavam inativos. O roteiro
anterior não verificava a nova dependência antes de desempacotar o executor,
e o comando fornecido não a instalava.

`apt-get --simulate --no-remove --no-install-recommends install crun` indicou
a instalação de `crun 1.14.1-1` e configuração de runtime3, com zero remoções
e zero atualizações de outros pacotes. Naquela rodada, a instalação real dependia
da senha local: `sudo -n true` foi recusado pelo sistema por exigir senha.

Os roteiros de runtime e builder agora verificam se o pacote está configurado
e se `/usr/bin/crun` está executável antes de instalar qualquer pacote. A recusa
informa o comando de recuperação sem copiar saídas potencialmente privadas.
O CI instala `crun` explicitamente; os guias incluem essa dependência e o comando
de recuperação. Não houve alteração dos binários nem reconstrução dos `.deb`.

Passaram os oito testes de `tests/test_runtime_probe.py`, incluindo cinco casos
de dependência ausente, não configurada, executável indisponível e instalação
pronta (inclusive pacote retido em sua versão pelo APT). Ruff e sua verificação
de formatação passaram nos arquivos alterados.
A verificação prévia também foi executada sem privilégios neste host e retornou
a mensagem esperada de `crun` ausente, sem alterar a instalação.

F3-01 ficou homologada no CI do marco 20 e pendente neste host naquela rodada.
O operador instalou a dependência e repetiu o ensaio conforme
[VARIAVEIS_SEGREDOS.md](VARIAVEIS_SEGREDOS.md); o resultado está no marco 22.
Os três checks preliminares desta tentativa não substituem os testes de entrega,
rotação e remoção de segredos.

## Marco 22 — Variáveis e segredos homologados localmente (F3-01)

O operador repetiu o roteiro em 21/10/2026, após instalar `crun`, usando o
checkout `9e50a9de8fe8ed3c007f8a12a680f7509de4f625`. As **102 verificações passaram**,
com `passed: true`, sem erro de limpeza nem repetições por executor ocupado.
Evidência: [secrets-runtime-local.json](evidencias/secrets-runtime-local.json),
cópia integral de `var/secrets-report.json`, iniciado às 11:34:43 UTC.
São as 99 verificações do CI do marco 20 mais a verificação prévia de `crun`,
checksum e identidade do pacote local do executor.

A consulta ao inventário confirmou os pacotes configurados: `shinyd-runtime
0.1.0~runtime3`, `shinyd-control 0.1.0~control2`, `shinyd-builder
0.1.0~builder2`, `crun 1.14.1-1` e Podman `4.9.3+ds1-1ubuntu0.2`. Os checksums
dos três `.deb` locais passaram e coincidem com os registrados no ensaio:

| Pacote local | SHA-256 do `.deb` |
|---|---|
| `shinyd-runtime 0.1.0~runtime3` | `30f2dfaf50ea57ad2e6b84de986e0cabbb0283269bb8a3f3e40f5096268771b5` |
| `shinyd-control 0.1.0~control2` | `d3c59d34dce5c035f903047931dcaf3490eea2bf7bc01738cd15684970440b81` |
| `shinyd-builder 0.1.0~builder2` | `c301c391636c27759072cf984a7c7b2838fb73bc69080dbe8e2f313f5a333dd9` |

Ambiente local: Ubuntu 24.04.5 LTS amd64, kernel `7.0.0-38-generic`, 16 CPUs
lógicas e 66.735.198.208 bytes de RAM. Os ambientes R 4.4.3/Shiny 1.10.0 e
R 4.5.2/Shiny 1.13.0 atenderam simultaneamente. Isso é um ensaio funcional,
sem estimativa de capacidade a partir da quantidade de verificações ou da RAM.

O navegador confirmou valores recebidos pelo R, incluindo Unicode e novas linhas,
revisão anterior mantida até a aplicação explícita, rotação sem reenviar ZIP
(uma única versão de código) e variável removida ausente após aplicar novamente.
A rota anterior retornou HTTP 404. Os demais apps mantiveram seus processos,
continuaram reativos e não receberam a variável do app testado. Os valores
sintéticos não apareceram nos metadados/logs dos containers, logs dos serviços
ou banco SQLite em texto claro; esse resultado não garante ocultação contra
código de app que deliberadamente exponha os próprios segredos.

Também passaram bloqueio de URL direta e WebSocket privados, acesso público,
cookies seguros, afinidade, upload/download e apresentação do painel em 360 px.
O reinício de painel/worker preservou o processo e a sessão Shiny; os PIDs de
auth/gateway/ingress permaneceram `865995`/`865996`/`865997` antes e depois.

A consulta posterior ao systemd confirmou painel, auth, worker, gateway,
ingress, manutenção, builder e runtime inativos, com resultado `success`.
Seus sockets estavam inativos e `shinyd.target` desabilitado. O socket do agente
de diagnóstico permaneceu escutando. Os pacotes ficam instalados, mas o ensaio
descartável não deixa o painel configurado para atendimento permanente.

Isso conclui a homologação local de F3-01. Certificado e nomes são de teste;
DNS público, emissão ACME, reboot, capacidade e QoS sob carga não foram testados.
F3-02 é o próximo marco: volumes persistentes por app, preservados entre
publicações e isolados dos demais apps. Rollback com dados, drenagem e demais
itens da fase 3 continuam no backlog.

## Marco 23 — Dados persistentes por app (F3-02), homologados no CI

Implementados volume por UUID de app, habilitação autorizada e confirmada pelo
painel, diretório `SHINYD_DATA_DIR`, preservação entre publicações e reserva de
um único escritor pelo executor. A candidata usa tmpfs vazio; o worker registra
etapas duráveis, encerra o teste e só libera os dados reais depois da remoção
confirmada da publicação anterior. Essa dependência antecipa a interrupção
imediata do ciclo de vida; drenagem continua pendente. A interface informa falhas
antes e depois da interrupção sem prometer reversão de dados.

O runtime usa bind mount com `idmap`, sem troca recursiva de donos dos arquivos.
Raiz e diretórios por app são privados de root; apenas a pasta de arquivos é
montada. Paths/opções de montagem não são parâmetros da API. A remoção do
container não remove o volume. Antes de montar, o executor exige 1 GiB livre;
isso não implementa quota nem protege contra esgotamento posterior do disco.

Pacotes `runtime4` e `control3`, com migração `0008`; builder2 preservado.
Passaram **253 testes Python**, Go com detector de concorrência, `go vet`, Ruff,
verificação de formatação, consistência de migrações e diagnóstico Django de
produção. Os testes cobrem autorização, cancelamento antes/depois da troca,
falha de candidata/partida real, resposta perdida, worker antigo, remoção
inconclusiva, reserva de escritor após recuperação e links maliciosos.

O roteiro integrado foi ampliado para escrita real pelo Shiny, publicação de
outro ZIP, reinício por configuração, falha de candidata que escreve somente
na pasta temporária, dois apps com volumes isolados e remoção que preserva
arquivos. A [primeira execução](https://github.com/rfsaldanha/shinyd/actions/runs/39597869759),
no commit `f6acb5aa50da7a33d99641f56ee8388b3cb53e69`, passou nas **117 verificações**,
com todos os cinco jobs aprovados, sem erro de limpeza nem repetições por executor
ocupado.

A [execução final](https://github.com/rfsaldanha/shinyd/actions/runs/39598445721),
no commit `f323fcd2d43ce0d9a3ed1f045a004590e9bf763e`, também passou nas **117 verificações**
e em **todos os cinco jobs**. O relatório integral do job `119216524153` está em
[data-runtime-ci.json](evidencias/data-runtime-ci.json). Não houve erro de limpeza
nem repetições por executor ocupado. A segunda execução confirmou mapeamentos
de usuário distintos entre os apps e registrou o sistema de arquivos ext4.

O navegador comprovou gravação no volume, nova publicação de código preservando
o arquivo, reinício por configuração preservando dados, recusa de segundo
escritor, dois apps sem acesso aos arquivos um do outro e remoção de container
sem excluir o volume. Uma candidata escreveu em sua pasta temporária e falhou:
o arquivo não apareceu no volume real e a publicação anterior manteve rota e PID.
Links para o caminho da chave de controle permaneceram inacessíveis no container;
a chave no host permaneceu intacta. Os outros apps mantiveram processos e
reatividade durante as publicações.

Ambiente: runner Ubuntu 24.04, kernel `7.0.0-1017-azure`, 4 CPUs lógicas,
16.760.344.576 bytes de RAM, Podman 4.9.3, crun 1.14.1 e ext4. Os mapeamentos
foram `0 327680 65536` e `0 393216 65536`; os arquivos de teste em disco
pertenciam a UID 65534, conforme a verificação aprovada. Essa medição funcional
não homologa reboot, capacidade, esgotamento de disco, quotas ou rollback de dados.
O job também verificou separadamente que remover o pacote de controle preserva
seu arquivo de estado.

A migração `0008` foi aplicada ao banco de desenvolvimento depois do backup
privado (modo 0600) `var/backups/metadata-before-persistence-20261021T121336Z.sqlite3`.
Foram construídos e conferidos os pacotes locais:

| Pacote local | SHA-256 do `.deb` |
|---|---|
| `shinyd-runtime 0.1.0~runtime4` | `d34a45541dc2196b244961e1ea9ea67bf03cbd422da20f71b2e83a3f43f1706d` |
| `shinyd-control 0.1.0~control3` | `0d9f31ff1a92c796f5874ee9eb2d817561cf1ddce2b8e4e26a1861dc39c00f63` |

O builder2 local continua com hash
`c301c391636c27759072cf984a7c7b2838fb73bc69080dbe8e2f313f5a333dd9`.
O CI constrói seus próprios arquivos, identificados pelo relatório; não se
declara igualdade binária com os pacotes locais. Naquela rodada, a homologação
neste host dependia da execução pelo operador: `sudo -n true` informou que
exigia senha. Os pacotes instalados no host ainda não tinham sido atualizados;
a execução posterior está registrada no marco 24.

Contrato, limitações e comando: [DADOS_PERSISTENTES.md](DADOS_PERSISTENTES.md).

## Marco 24 — Dados persistentes homologados localmente (F3-02)

O operador executou o roteiro em 21/10/2026, com checkout `2caefac`.
As **119 verificações passaram**, com `passed: true` e sem erro de limpeza.
Evidência: [data-runtime-local.json](evidencias/data-runtime-local.json), cópia
integral de `var/data-report.json`, iniciado às 12:38:06 UTC. São as 117
verificações do CI do marco 23 mais checksum e identidade do pacote local do
executor. O teste `activation_cross_app_artifact_denied` recebeu uma resposta
`runtime_busy`, repetiu a tentativa e confirmou `artifact_rejected`. Não houve
outras repetições registradas por executor ocupado.

O inventário confirmou os pacotes configurados: `shinyd-runtime
0.1.0~runtime4`, `shinyd-control 0.1.0~control3`, `shinyd-builder
0.1.0~builder2`, `crun 1.14.1-1` e Podman `4.9.3+ds1-1ubuntu0.2`.
Os checksums dos três `.deb` locais passaram e coincidem com os registrados
no ensaio e no marco 23. Os pacotes do CI têm seus próprios hashes.

Ambiente local: Ubuntu 24.04.5 LTS amd64, kernel `7.0.0-38-generic`, 16 CPUs
lógicas, 66.735.198.208 bytes de RAM e ext4. Os containers persistentes usaram
mapeamentos distintos: `0 655360 65536` e `0 720896 65536`. O teste confirmou
arquivos em disco pertencentes a UID 65534 e diretórios pais privados de root.
Também atenderam simultaneamente os ambientes R 4.4.3/Shiny 1.10.0 e
R 4.5.2/Shiny 1.13.0. O perfil foi funcional, com builds offline e operações
reativas; não se deriva capacidade da quantidade de verificações ou da RAM.

O navegador confirmou habilitação da persistência, gravação real pelo Shiny,
troca de código e reinício por configuração preservando os arquivos. O executor
recusou um segundo escritor do mesmo app. Uma candidata que escreveu na pasta
temporária e falhou não alterou o volume real, a rota nem o PID da publicação
anterior. Arquivos temporários foram descartados na troca; links no volume
permaneceram inacessíveis para alcançar a chave de controle, que não foi
alterada. Dois apps mantiveram volumes isolados; remover o container de um
preservou seus arquivos e não interrompeu o outro.

As 63 verificações da instalação incluíram acesso público/privado, bloqueio de
URL direta e WebSocket não autorizados, afinidade, upload/download, variáveis e
segredos e painel em tela de 360 px. Reiniciar painel/worker preservou a sessão
Shiny e os PIDs de auth/gateway/ingress: `939509`/`939510`/`939511` antes e depois.
As demais aplicações permaneceram reativas durante as publicações.

A consulta posterior ao systemd confirmou painel, auth, worker, gateway,
ingress, manutenção, builder e runtime inativos, com resultado `success`.
Os sockets de painel, auth, gateway, builder e runtime estavam inativos;
`shinyd.target`, desabilitado. O socket do agente de diagnóstico permaneceu
escutando. O ensaio descartável removeu seus dados de teste e não deixa uma
instalação configurada para atendimento permanente.

F3-02 está homologada no CI e neste host. Certificado e nomes são de teste;
DNS público, emissão ACME, reboot, capacidade, QoS sob carga e esgotamento de
disco não foram testados. O próximo marco é **F3-03: histórico, ativação e
rollback de versões**, preservando dados persistentes e configuração atual.
Rollback de código não reverte escritas ou migrações de dados feitas pelo app;
drenagem e backup recuperável continuam no backlog.

## Marco 25 — Histórico e rollback de código (F3-03)

Implementado histórico de versões e publicações por app, com paginação de 20
registros em cada lista e acesso restrito a quem pode publicar. O painel exibe
estado, identificação da versão, imagem, responsável, revisão de configuração
e resultado da tarefa. Publicações encerradas oferecem retorno explícito ao
código, com confirmação sobre interrupção e compatibilidade dos dados.

O retorno cria uma publicação nova e reutiliza a imagem preparada, mantendo a
configuração desejada atual, inclusive mudanças pendentes. Segredos antigos não
são restaurados e variáveis removidas não reaparecem. A confirmação assinada
expira em 30 minutos e é recusada se o app, a configuração, a publicação ativa
ou o destino diferirem do estado confirmado. Idempotência, autorização,
cancelamento e checkpoints usam o fluxo de ativação existente.

`restored_from` registra a origem do retorno; `previous` mantém a publicação
substituída. Auditoria registra IDs e revisões, sem conteúdo dos segredos.
O volume persistente é aberto apenas depois da remoção confirmada do escritor
anterior. Voltar código não desfaz dados, nem garante compatibilidade com seu
formato atual. Drenagem e backup automatizado continuam pendentes.

Passaram **273 testes Python**, Ruff, formatação, consistência de migrações e
diagnóstico Django de produção. O teste de navegador sem privilégios passou
com login, cadastro, upload, inspeção, atualização automática, histórico e tela
de 360 px. Capturas locais: `/tmp/shinyd-evidence/history-desktop.png` e
`history-mobile.png`. A migração `0009` foi aplicada ao banco de desenvolvimento
após backup SQLite privado (0600):
`var/backups/metadata-before-rollback-20261021T131405Z.sqlite3`.

O pacote de controle passa a `control4`; executor `runtime4` e builder `builder2`
permanecem. O roteiro instalado foi ampliado para rollback no navegador com
outro app em uso, confirmação desatualizada, dados gravados e segredo removido.
Os resultados da homologação integrada deste fluxo estão registrados abaixo.
A execução local exige senha de sudo no terminal do operador, conforme
`sudo -n true`.

Também foi aberta uma prévia local solicitada pelo operador, em
`http://127.0.0.1:8010`, com banco próprio em `var/interface-preview`. Não executa
containers nem altera o banco de desenvolvimento. A migração nessa prévia
preservou contas e dados, após backup privado. Credenciais ficam apenas no
arquivo local `var/interface-preview/ACESSO.txt`, fora do Git.

Contrato e comando de homologação: [HISTORICO_ROLLBACK.md](HISTORICO_ROLLBACK.md).

A [primeira execução integrada](https://github.com/rfsaldanha/shinyd/actions/runs/39606821149),
no commit `3a4e4eb1b9a28b2005bc09de7904cac8975ccd50`, aprovou os jobs Python,
Go, agente e runtime, mas o job de publicação parou por prazo ao aguardar
recursos para a candidata de rollback. A evidência está em
[rollback-admission-ci.json](evidencias/rollback-admission-ci.json): o worker
permaneceu em `waiting_runtime`, sem erro de limpeza. O histórico em 360 px e a
recusa de confirmação desatualizada já haviam passado.

O roteiro mantinha dois apps de referência e dois persistentes, ocupando seus
quatro slots. A candidata temporária exigia um quinto slot. Foi corrigida apenas
a política do ensaio, para cinco instâncias, preservando o orçamento agregado
de 4096 MiB e a admissão real do executor. Isso não muda limites de uma
instalação existente, nem autoriza ignorar pressão de memória. A homologação
integrada foi repetida com espaço para executar o cenário proposto.

A [execução corrigida](https://github.com/rfsaldanha/shinyd/actions/runs/39609436973),
no commit `ec8f242d30df8b2eb034f278664f4a899c3aae4b`, passou em **todos os cinco
jobs** e nas **128 verificações integradas**, incluindo as 74 da instalação.
O [relatório integral](evidencias/rollback-runtime-ci.json) do job `119254104926`
registra `passed: true`, sem erro de limpeza nem repetições por executor ocupado.
O ensaio iniciou às 13:38:54 UTC, em 21/10/2026.

O navegador confirmou retorno à imagem da versão anterior sem novo ZIP/build,
nova publicação com origem registrada no histórico, arquivo persistente mantido,
configuração pendente aplicada e segredo removido ausente no R. A confirmação
aberta antes de uma alteração de configuração foi recusada; depois de reabri-la,
o retorno concluiu. O escritor anterior foi removido e sua rota retornou HTTP
404. Outra sessão Shiny manteve identidade, PID e contador, respondendo a uma
nova interação. O histórico passou em tela de 360 px. As verificações anteriores
de acesso privado, WebSocket, upload/download e segredos também passaram.

Ambiente: runner Ubuntu 24.04, kernel `7.0.0-1017-azure`, 4 CPUs lógicas,
16.760.344.576 bytes de RAM, Podman 4.9.3, crun 1.14.1 e ext4. Os mapeamentos
observados no teste de isolamento foram `0 327680 65536` e `0 393216 65536`.
O reinício de painel/worker preservou auth/gateway/ingress com PIDs
`11640`/`11641`/`11642`. O job verificou também que remover o pacote de controle
preserva seu arquivo de estado. Isso não mede capacidade, reboot, QoS sob carga,
compatibilidade de migrações de dados dos apps ou recuperação de backup.

O pacote local `shinyd-control 0.1.0~control4`, construído a partir de `3a4e4eb`
(mesmo código de produto, antes da correção do roteiro), teve checksum conferido:
`23be3efb0bf0721ba955c8201f00c29751c99fa97eef0426340d94a492d88324`.
O pacote construído no CI tem hash
`e5e8856542d231095723b815826e25365bbe22fc3bf6c0ecae2a86be7a16469d`;
não se declara igualdade binária. Os pacotes locais `runtime4` e `builder2`
continuam com os hashes do marco 23.

Naquela rodada, F3-03 estava implementada e homologada no CI. A homologação local
dos containers ainda aguardava o roteiro em [HISTORICO_ROLLBACK.md](HISTORICO_ROLLBACK.md),
que grava `var/rollback-report.json`. O controle instalado no host ainda era
`control3`; sua atualização e homologação posteriores estão no marco 26. A prévia
local teve login confirmado e captura em `var/interface-preview/catalogo.png`.
O próximo marco de implementação é F3-04: telemetria de sessões e drenagem.

## Marco 26 — Histórico e rollback homologados localmente (F3-03)

O operador executou o roteiro em 21/10/2026, com checkout `29cf8c6`.
As **130 verificações passaram**, com `passed: true`, sem erro de limpeza nem
repetições por executor ocupado. Evidência:
[rollback-runtime-local.json](evidencias/rollback-runtime-local.json), cópia
integral de `var/rollback-report.json`, iniciado às 14:05:11 UTC. São as 128
verificações do CI do marco 25 mais checksum e identidade do pacote local do
executor; as 74 verificações da instalação também passaram.

O inventário confirmou os pacotes configurados: `shinyd-runtime
0.1.0~runtime4`, `shinyd-control 0.1.0~control4`, `shinyd-builder
0.1.0~builder2`, `crun 1.14.1-1` e Podman `4.9.3+ds1-1ubuntu0.2`.
Os checksums dos três `.deb` locais passaram e coincidem com o ensaio.
O controle tem hash
`23be3efb0bf0721ba955c8201f00c29751c99fa97eef0426340d94a492d88324`,
conforme o marco 25; runtime e builder mantêm os hashes do marco 23.
Os pacotes construídos no CI têm seus próprios hashes.

Ambiente local: Ubuntu 24.04.5 LTS amd64, kernel `7.0.0-38-generic`, 16 CPUs
lógicas, 66.735.198.208 bytes de RAM e ext4. Os mapeamentos observados no teste
de isolamento foram `0 655360 65536` e `0 720896 65536`. O ensaio é funcional,
com builds offline e operações reativas; não mede capacidade de atendimento.

O navegador confirmou retorno à imagem anterior sem novo ZIP/build, uma nova
publicação com origem registrada no histórico e preservação do arquivo gravado.
A configuração pendente foi aplicada e o segredo removido permaneceu ausente
no R. A confirmação aberta antes de alterar a configuração foi recusada; o
retorno concluiu depois de reabri-la. O escritor anterior foi removido e sua
rota retornou HTTP 404. Outra sessão Shiny manteve identidade, PID e contador,
respondendo a uma nova interação. O histórico passou em tela de 360 px.

Também passaram acesso público/privado, bloqueio de URL direta e WebSocket não
autorizados, afinidade, upload/download e verificações de segredos. Reiniciar
painel/worker preservou a sessão Shiny; auth/gateway/ingress mantiveram os PIDs
`1057059`/`1057060`/`1057061` antes e depois.

A consulta posterior ao systemd confirmou painel, auth, worker, gateway,
ingress, manutenção, builder e runtime inativos, com resultado `success`.
Seus sockets e o timer de manutenção estavam inativos; `shinyd.target`,
desabilitado. O socket do agente de diagnóstico permaneceu escutando.
O pacote fica instalado, mas o ensaio não deixa o atendimento permanente
configurado. A prévia independente em `http://127.0.0.1:8010` continuou
respondendo a `/healthz` com HTTP 200.

F3-03 está homologada no CI e neste host. Certificado e nomes são de teste;
DNS público, emissão ACME, reboot, capacidade, QoS sob carga e recuperação de
backup não foram testados. Rollback de código não reverte escritas nem migrações
de dados feitas pelo app. O próximo marco é **F3-04: telemetria de sessões e
drenagem**, para permitir operações que aguardem o encerramento das sessões.

### Ponto combinado para teste manual pela interface

Em 22/10/2026, o usuário pediu para ser avisado quando a **Fase 3 estiver
concluída e uma instalação permanente de testes estiver disponível**. Dar esse
aviso ao atingir o marco, apresentando endereço, instruções de acesso e roteiro
para publicar e administrar apps reais. O aviso depende de:

- Concluir e validar F3-04 a F3-07, incluindo drenagem, operações de ciclo de
  vida, proteção contra ciclos de reinício e backup com restauração comprovada.
- Manter painel, worker, gateway, builder e executor configurados para uso
  contínuo no ambiente de testes, com HTTPS, conta de acesso e catálogo de
  ambientes adequado aos apps escolhidos.
- Verificar pelo navegador publicação, configuração, persistência, atualização
  e rollback nesse ambiente, documentando as limitações observadas.

A prévia local com containers desabilitados e os ensaios descartáveis não
atendem sozinhos a esse marco. Concorrência e QoS permanecem como testes da
Fase 4; o aviso não significa prontidão para produção. O marco ainda não foi
atingido.

## Marco 27 — Telemetria de conexões e drenagem (F3-04)

Em 22/10/2026, implementada a [troca com espera](DRENAGEM_SESSOES.md) para
ativação, aplicação de configuração e rollback. O pacote é `control5`, com
migração `0010`; executor `runtime4` e builder `builder2` permanecem iguais.
O formulário permite troca imediata ou espera de até cinco minutos. Durante
a espera, novas conexões WebSocket são recusadas, as existentes continuam
autorizadas e requisições HTTP em curso também impedem a troca. Dados
persistentes continuam com um único escritor.

O gateway reserva cada requisição antes de encaminhá-la, confirma a abertura
WebSocket após HTTP 101 e confirma o encerramento ao terminar o encaminhamento.
Renovações verificam autorização e telemetria. Ausência de confirmação não é
convertida em zero conexões; telemetria incerta aborta a troca preservando a
publicação anterior. Cancelamento, prazo esgotado, alteração de configuração
ou revogação do publicador também abortam a troca. Reiniciar o worker mantém
o prazo e o bloqueio no banco. `/v1/authorize` deixa de ser aceito: atualizações
exigem a janela e a atualização conjunta dos componentes de controle.

O painel diferencia WebSockets abertas, aberturas pendentes e HTTP em curso.
Essas medidas não são contagem de pessoas ou sessões internas do R. Não há
garantia de conclusão de tarefas R independentes ou de callbacks depois de
fechar a conexão, nem recuperação de sessão após queda de rede. Estatísticas
históricas e custo do registro no SQLite sob carga continuam pendentes.

Validação local sem privilégios:

- 284 testes Python passaram, incluindo público/restrito, revogação,
  cancelamento, prazo, telemetria incerta, transferência e escritor persistente.
- O teste com SQLite em arquivo e duas conexões reais confirmou a serialização
  entre admissão e troca, nas duas ordens, usando `IMMEDIATE` como em produção.
- `go vet ./...` e `go test -race ./...` passaram. O gateway foi verificado
  com WebSocket real, bloqueio de nova conexão e HTTP longo que perde telemetria.
- Ruff, formatação, consistência de migrações e Django `check --deploy` passaram.
- O teste de navegador sem privilégios passou em desktop e 360 px.

Antes de migrar os bancos locais, cópias SQLite consistentes foram gravadas em
`var/backups/metadata-before-drain-20261022T115852Z.sqlite3` e
`var/backups/preview-before-drain-20261022T115852Z.sqlite3`, com modo 0600.
A prévia em `http://127.0.0.1:8010` foi reiniciada e respondeu HTTP 200 em
`/healthz`; mantém contas, dados e execução de containers desabilitada.

No CI, passaram **137 verificações integradas**, sendo **83** do roteiro
instalado, e os cinco jobs concluíram com sucesso. Execução
[39775184217](https://github.com/rfsaldanha/shinyd/actions/runs/39775184217),
commit `fd8c9112aac380ce729a2ee781dd43dac7b924a0`, job builder `119812203593`.
A evidência completa está em [drain-runtime-ci.json](evidencias/drain-runtime-ci.json).
Não houve erro de limpeza nem repetição por executor ocupado.

O navegador confirmou sessão Shiny reativa durante a drenagem, recusa de
nova conexão autorizada, upload/download, cancelamento/reabertura, reinício
do worker, troca após a última conexão, volume preservado, outra sessão não
interrompida e telemetria em tela de 360 px. Também passaram os cenários
anteriores de acesso público/restrito, segredos, persistência e rollback.
O teste de API interna agora verifica `/v2/request` na entrada pública.

Ambiente do CI: runner Ubuntu 24.04, kernel `7.0.0-1017-azure`, quatro CPUs
lógicas, 16.760.344.576 bytes de RAM, Podman 4.9.3, crun 1.14.1 e ext4.
O SHA-256 do `control5` instalado no CI foi
`211187f083279d5c861e827bef4e5cf1ebe8ce9cfde3d93c72285172e6d3c0e3`.
O pacote construído localmente tem seu próprio SHA-256:
`76bc9947cb7ec8abf885ed587449d7f8cb1a34f97424aee6a6ebbe279f1648b6`.
Os três pacotes locais passaram na conferência de checksum.

Naquela rodada, a homologação local com containers permanecia pendente.
`sudo -n true` informou que era necessária senha no host; nenhuma instalação
privilegiada foi tentada pelo agente. O usuário executou depois o comando de
[DRENAGEM_SESSOES.md](DRENAGEM_SESSOES.md), com o resultado registrado no marco 28.

O próximo item é F3-05, operações individuais e em lote. O marco de teste
manual combinado com o usuário ainda depende de F3-05 a F3-07 e de uma
instalação permanente de testes; esta entrega não constitui esse aviso.

## Marco 28 — Drenagem homologada localmente (F3-04)

O usuário executou o roteiro instalado com `runtime4`, `builder2` e `control5`.
O relatório `var/drain-report.json`, com início registrado em
`2026-10-22T12:19:31.369865+00:00`, foi conferido e preservado byte a byte em
[drain-runtime-local.json](evidencias/drain-runtime-local.json), com 34.304 bytes.
Passaram **139 verificações**, incluindo as **83** do roteiro instalado e
as **13** de ativação. Não houve erro de limpeza nem repetição por executor
ocupado. Em relação ao CI, as duas verificações adicionais conferem checksum
e identidade do pacote local do executor; os demais nomes de verificações
coincidem com os do ensaio aprovado do marco 27.

O SHA-256 do pacote `control5` instalado corresponde ao artefato local:
`76bc9947cb7ec8abf885ed587449d7f8cb1a34f97424aee6a6ebbe279f1648b6`.
Os checksums de `runtime4` e `builder2` também foram conferidos, sem alterações.
O sistema registra os três pacotes como instalados e configurados.

Ambiente observado: Ubuntu 24.04.5 LTS amd64, kernel `7.0.0-38-generic`,
16 CPUs lógicas, 66.735.198.208 bytes de RAM, Podman 4.9.3, crun 1.14.1 e ext4.
Os mapeamentos de usuários observados foram `0 655360 65536` e
`0 720896 65536`. O ensaio é funcional; não mede capacidade de atendimento.

O navegador confirmou a sessão Shiny reativa durante a drenagem, o bloqueio
de nova conexão autorizada e upload/download na sessão existente. Cancelar
reabriu a admissão na publicação anterior. Uma segunda espera sobreviveu ao
reinício do worker e a troca concluiu após fechar a última conexão, mantendo
o arquivo persistente e removendo o escritor anterior. Outra sessão Shiny
permaneceu reativa. A telemetria foi verificada em tela de 360 px.

Também passaram os cenários de autenticação, isolamento, segredos, persistência
e rollback. No teste de reinício de painel/worker, os PIDs de auth, gateway
e ingress permaneceram `1978473`, `1978474` e `1978475`, respectivamente.
A rota da publicação anterior no teste de configuração retornou HTTP 404.

A consulta posterior ao systemd confirmou painel, auth, worker, gateway,
ingress, manutenção, builder e runtime inativos, com resultado `success`.
Seus sockets e o timer de manutenção estavam inativos; `shinyd.target`,
desabilitado. O socket do agente de diagnóstico permaneceu escutando.
A prévia independente em `http://127.0.0.1:8010` respondeu HTTP 200 em
`/healthz`. O ensaio não deixa configurado o atendimento permanente.

F3-04 está homologada no CI e neste host, com os limites de telemetria de
conexões descritos no marco 27. O próximo item é **F3-05: iniciar, parar e
reiniciar apps individualmente ou em conjunto**. O ponto combinado para
testar os apps do usuário ainda exige F3-05 a F3-07 e a instalação permanente
de testes; não foi atingido nesta homologação.

## Marco 29 — operações individuais e em lote (F3-05)

Implementados iniciar, parar e reiniciar pela interface, para um app, uma
seleção ou todos sob a gestão do usuário. A confirmação apresenta o conjunto
e o impacto, com modo imediato ou espera de conexões. Os resultados são
individuais; recusas de permissão, configuração alterada, fila cheia ou outra
operação pendente não impedem enviar os demais apps. Repetir a confirmação
retorna o mesmo lote. Há até 100 apps por lote e 100 tarefas globais pendentes.

Iniciar usa a última versão publicada com sucesso e a configuração atual;
reiniciar reutiliza o código ativo, inclusive sem mudanças, em nova instância.
Parar mantém o volume, a configuração e a versão. A parada registra a retirada
da rota antes de chamar o executor e retoma a remoção com o mesmo ID após perda
de resposta ou reinício do worker. Depois desse ponto, cancelamento e revogação
não abandonam a limpeza. Uma nova partida aguarda a remoção confirmada.

O pacote é `control6`, com migração `0011`, compatível com `runtime4` e `builder2`.
A migração retém a versão da publicação ativa; não escolhe automaticamente
uma tentativa antiga para apps sem publicação ativa. O funcionamento, limites
e roteiro estão em [OPERACOES_APPS.md](OPERACOES_APPS.md).

Verificações locais executadas em 22/10/2026:

- **300 testes Python aprovados**, incluindo migração sobre dados existentes,
  falhas parciais de lote, CSRF, permissões, repetição, capacidade da fila,
  parada imediata/com espera, cancelamento, timeout, telemetria incerta,
  perda de resposta e execução antiga do worker.
- O teste de concorrência usa SQLite em arquivo com opções de produção e
  duas conexões reais, nas duas ordens de admissão e retirada da rota, para
  ativação e parada.
- Ruff, formatação, consistência de migrações, `check --deploy` e `git diff --check`
  aprovados. O teste de navegador passou em 360 px, incluindo seleção,
  confirmação e resultado de operação com execução desabilitada.
- Após o ajuste final da apresentação, os 24 testes de ciclo de vida/drenagem
  e o roteiro de navegador passaram novamente.
- Pacote local construído. SHA-256 de `shinyd-control_0.1.0~control6_amd64.deb`:
  `2bd91c8a99f61e9b8651f80a4ed17aa2ffcc1334189d129bbfd32449f2dc5316`.

Os bancos de desenvolvimento e da prévia receberam `0011` após backup SQLite
em `var/backups/metadata-before-lifecycle-20261022T134004Z.sqlite3` e
`var/backups/preview-before-lifecycle-20261022T134004Z.sqlite3`, ambos 0600.
A prévia na porta 8010 foi reiniciada preservando conta, dados e chaves;
`/healthz` respondeu HTTP 200.

O roteiro instalado foi ampliado para reiniciar um app mantendo outro reativo,
parar com espera e reiniciar o worker, iniciar uma seleção com app já ativo,
reiniciar ambos, parar todos e voltar a iniciar um. O resultado do CI está
registrado abaixo; a homologação local deste pacote continua pendente.
O comando local está em [OPERACOES_APPS.md](OPERACOES_APPS.md); `sudo -n true`
ainda exige senha neste host.

O próximo item é **F3-06: recuperação de falhas com limite de tentativas**.
O marco combinado de teste manual completo ainda depende também de F3-07
e de uma instalação permanente de testes; não foi atingido nesta etapa.

A primeira execução do [CI deste marco](https://github.com/rfsaldanha/shinyd/actions/runs/39789436763)
aprovou Python, Go, agente e executor. O ensaio instalado chegou ao primeiro
reinício, confirmou a conclusão, a nova instância e o dado preservado, mas
falhou na chamada `APIRequestContext.get` que consultava a rota anterior.
Esse cliente não herda a configuração de DNS/SPKI passada ao Chrome no fixture.
A verificação foi alterada para uma aba separada do navegador, preservando a
sessão em uso e o critério HTTP 404. Os fluxos posteriores ainda não haviam
sido executados nessa primeira tentativa; não houve erro de limpeza reportado.

### Evidência integrada após a correção do roteiro

O [segundo ensaio](https://github.com/rfsaldanha/shinyd/actions/runs/39792232209),
no commit `865e8be5fd97d0ebc967876f58cedcbe78b1ea22`, aprovou as **155 verificações
integradas**, incluindo **101 da instalação/navegador** e 13 da ativação.
O relatório completo está em
[lifecycle-runtime-ci.json](evidencias/lifecycle-runtime-ci.json). Não houve erro
de limpeza, repetição por executor ocupado ou repetição da API do executor.

O navegador confirmou nova instância no reinício individual, HTTP 404 na rota
anterior, outra sessão ainda reativa, parada com espera retomada após reinício
do worker e ausência de partida automática ao visitar o app parado. Iniciar
uma seleção reutilizou a versão, os dados e a configuração, sem reiniciar o
app já ativo. Reiniciar a seleção trocou as duas instâncias; parar todos removeu
ambas. Uma nova partida manteve o dado persistente. Os resultados couberam em
360 px. As verificações anteriores de acesso, segredos, dados e rollback também
passaram.

O runner registrou kernel `6.17.0-1022-azure`, arquitetura x86_64, quatro CPUs
lógicas, 16.766.414.848 bytes de RAM, Podman 4.9.3, crun 1.14.1 e ext4. Os mapas
de usuário observados no teste de persistência foram `0 327680 65536` e
`0 393216 65536`. Esse perfil não é um teste de capacidade.

O SHA-256 do `control6` usado no CI foi
`377a47c9134f0fc42e770e6df8c0ca2a84d65d4240e4c82d83de3373933d0bdf`.
O pacote construído localmente tem o checksum registrado acima; são construções
distintas, não uma afirmação de identidade binária. A correção do roteiro não
altera o conteúdo do pacote local.

Na consulta ao GitHub, Python, Go, agente e executor já constavam como
`completed/success`. Todas as etapas do job builder, incluindo o ensaio,
a remoção do pacote preservando estado e o envio dos artefatos, também constavam
como `completed/success`; o estado agregado desse job ainda era `in_progress`.
Essa distinção está preservada nos metadados da evidência, sem antecipar a
conclusão administrativa do workflow.

Ainda não existe `var/lifecycle-report.json` neste host. O pacote local está
pronto e seu checksum foi conferido; a homologação com `sudo` permanece para
o operador. O ensaio é descartável e não deixa configurado o atendimento
permanente. O marco de teste manual completo combinado não foi atingido:
continuam necessários F3-06, F3-07 e a instalação permanente de testes.

## Marco 30 — diagnóstico da primeira homologação local de F3-05

O relatório produzido pelo operador em 22/10/2026, iniciado às
`15:53:05.078500+00:00`, foi lido e preservado integralmente em
[lifecycle-runtime-local-initial.json](evidencias/lifecycle-runtime-local-initial.json).
**A homologação não passou.** Há 55 verificações no nível principal, sendo
`installed_https_probe` a única reprovada; o ensaio instalado registrou 45
verificações aprovadas, incluindo sua coleta de IDs para limpeza, e a ativação
aprovou as 13 verificações. Não há erro de limpeza reportado. O teste parou
na exigência `data_second_writer_denied`, antes dos novos fluxos de operações;
não comprovou esses fluxos neste host. A ausência do arquivo registrada no
marco anterior descreve a situação anterior à execução do operador.

O [evento correspondente do journal](evidencias/lifecycle-runtime-local-diagnostic.json)
mostra HTTP 429 às 12:58:01, horário local, no `ensure` da instância concorrente
`bb30cd78-f4c0-4a7f-ab50-56dc81eaccee`. O relatório antigo não guardava o código
da recusa. Pelo código de `Manager.Ensure`, com volume ocupado e menos de 256
registros, `runtime_busy` pode preceder `data_in_use`; os demais controles de
admissão são posteriores à verificação do escritor. A atribuição a executor
ocupado é uma inferência apoiada nessa ordem e no HTTP observado, não um código
de resposta capturado pelo relatório antigo. O processo ativo continuava
presente nos diagnósticos; não há evidência de segundo escritor iniciado.

Corrigido o roteiro para repetir somente `runtime_busy`, no máximo quatro
tentativas com o mesmo ID e três esperas de cinco segundos. Aceitação, outra
recusa, executor indisponível, resposta inválida ou ocupação persistente
continuam reprovando o teste. Somente `data_in_use` o aprova. O relatório passa
a preservar `installed.data_second_writer_outcomes`, sem valores de ambiente,
e `installed.failed_check`. Não se alterou o executor, o painel, o contrato
de isolamento, o banco ou os pacotes.

Verificações executadas após a correção:

- **310 testes Python aprovados**, incluindo dez cenários novos que executam
  o mesmo código enviado a `shinyd-admin`, com o cliente do executor substituído
  para controlar as respostas, e conferem limites, identidade e ausência de
  segredo no diagnóstico. O conjunto específico com testes de instalação
  aprovou 34 casos.
- Ruff, formatação e `git diff --check` aprovados. O teste Go
  `TestOnlyConfirmedRemovalReleasesDataWriterAcrossRecovery` passou, comprovando
  a reserva do escritor até a remoção confirmada também após recuperação.
- Checksums de `control6`, `runtime4` e `builder2` conferidos; o `control6`
  instalado tem o mesmo SHA-256 local `2bd91c8a99f61e9b8651f80a4ed17aa2ffcc1334189d129bbfd32449f2dc5316`.
- Painel, auth, worker, gateway, ingress, manutenção, builder e runtime estavam
  inativos com `Result=success` após o ensaio. Seus sockets e timer estavam
  inativos, `shinyd.target` desabilitado e o socket de diagnóstico ativo.
  A prévia independente na porta 8010 respondeu HTTP 200 em `/healthz`.

Uma nova consulta ao CI do marco 29 confirmou os cinco jobs como
`completed/success`, incluindo builder. Isso encerra a pendência de estado
agregado registrada naquele marco; não substitui a nova execução local.
O ajuste do roteiro está pronto para repetição com o comando de
[OPERACOES_APPS.md](OPERACOES_APPS.md). O ambiente exige senha para `sudo`;
nenhuma execução privilegiada nova foi realizada pelo agente. F3-05 continua
sem homologação local concluída, e o marco de teste manual combinado ainda
depende de F3-06, F3-07 e da instalação permanente de testes.

## Marco 31 — F3-05 homologada localmente após o ajuste do roteiro

O operador repetiu o ensaio com o roteiro do commit
`82cfaa2d3583ec321bd0fbc5ba76e2c11d39a7dd`. O relatório iniciado em
`2026-10-22T16:35:07.306168+00:00` foi conferido e preservado integralmente em
[lifecycle-runtime-local.json](evidencias/lifecycle-runtime-local.json).
Passaram **157 verificações integradas**, incluindo **101 da instalação e
navegador** e **13 da ativação**. Não há erro de execução ou limpeza, nem
repetição na API do executor ou no ensaio de ativação. A verificação do segundo
escritor registrou `data_second_writer_outcomes: ["data_in_use"]`: a recusa
específica foi observada já na primeira tentativa. Isso conclui a pendência
local registrada no marco 30; a evidência da primeira falha foi mantida.

O navegador confirmou reinício individual com nova instância e dados
preservados, fechamento da rota anterior e outra sessão Shiny ainda reativa.
A parada com espera retomou após reinício do worker e concluiu ao fechar a
última conexão; visitar o app parado não o iniciou. Iniciar uma seleção
preservou o processo do app já ativo e restaurou versão, dados e configuração
do app parado. Reiniciar a seleção trocou ambas as instâncias; parar todos
removeu ambas. A nova partida preservou os dados. Os resultados couberam em
360 px, sem erro JavaScript observado. Também passaram acesso público/privado,
bloqueio por URL e WebSocket, segredos, persistência, rollback e drenagem.

O SHA-256 do `control6` observado na instalação corresponde ao artefato local:
`2bd91c8a99f61e9b8651f80a4ed17aa2ffcc1334189d129bbfd32449f2dc5316`.
Os checksums de `runtime4` e `builder2` também foram conferidos. Os três pacotes
constam instalados. O ambiente registrou kernel `7.0.0-38-generic`, arquitetura
x86_64, 16 CPUs lógicas, 66.735.198.208 bytes de RAM, Podman 4.9.3, crun 1.14.1
e ext4. Os mapas de usuários foram `0 655360 65536` e `0 720896 65536`.
No teste de reinício de painel/worker, auth, gateway e ingress mantiveram os
PIDs `2263394`, `2263395` e `2263396`. A rota anterior no teste de configuração
retornou HTTP 404. O ensaio não mede capacidade nem latência sob carga.

A consulta posterior ao systemd confirmou painel, auth, worker, gateway,
ingress, manutenção, builder e runtime inativos, com resultado `success`.
Os sockets correspondentes e o timer de manutenção estavam inativos;
`shinyd.target`, desabilitado. O socket de diagnóstico permaneceu ativo.
A prévia independente em `http://127.0.0.1:8010` respondeu HTTP 200 em `/healthz`.
O ensaio não deixou configurado atendimento permanente.

O [CI da correção](https://github.com/rfsaldanha/shinyd/actions/runs/39813293208)
também concluiu com os cinco jobs em `completed/success`. Seu
[relatório integrado](evidencias/lifecycle-runtime-ci-retry.json) aprovou
155 verificações, incluindo 101 de instalação/navegador e 13 de ativação, sem
erro de limpeza e sem repetição por executor ocupado. A diferença de duas
verificações no ensaio local decorre da instalação explícita do pacote runtime
por seu roteiro; o CI o instala em uma etapa anterior. O pacote `control6`
construído no CI tem SHA-256
`2880ef411cdd4e814cd176ec18f11b6426b347b02491d527a0f69d1984ee661b`;
é uma construção distinta do artefato local, não identidade binária.

F3-05 está homologada no CI e neste host. A próxima implementação é
**F3-06: recuperação de falhas com limite de tentativas**. O teste manual
completo combinado ainda depende de F3-06, F3-07 (backup/restauração básico)
e da instalação permanente de testes. Não foi anunciado como pronto neste
marco; o roteiro não testa reboot, emissão ACME, DNS público, capacidade,
QoS sob disputa ou quotas de disco. Esta rodada registra evidências e
documentação; não altera código ou pacotes nem repete testes já aprovados.

## Marco 32 — recuperação de falhas com circuito persistente (F3-06)

Implementada recuperação opcional por app, desabilitada por padrão, com
confirmação pela interface, tentativas visíveis e suspensão persistente.
O executor `runtime5` distingue processo comprovadamente encerrado/ausente,
falta de memória e erro de inspeção. O painel `control7`, migração `0012`,
observa um app por passagem do worker e admite até três tarefas de recuperação,
com esperas mínimas de 10, 30 e 60 segundos. Os detalhes de operação, limites
e comando de homologação estão em [RECUPERACAO_APPS.md](RECUPERACAO_APPS.md).

A contagem e o circuito sobrevivem ao reinício do worker. Depois de três
tentativas sem dez minutos estáveis observados, uma nova falha suspende a
recuperação até intervenção. Há também limite independente de três tarefas
automáticas em qualquer janela de dez minutos, inclusive após rearmar a
política. Fila cheia aguarda sem consumir uma tentativa. Falta de memória
suspende a recuperação, também durante a partida da candidata, após limpeza.
Configuração pendente e perda de permissão exigem intervenção. A recuperação
reutiliza a versão publicada e nunca restaura segredos antigos para ignorar
uma mudança de configuração.

Reservas e decisões são transacionais, com verificação da publicação e da
intenção atual. Operações manuais pendentes têm precedência. Desabilitar
cancela candidatas ainda não ativadas; a limpeza permanece retomável. Cada
tentativa usa nova publicação/instância e o fluxo existente de dados temporários,
retirada confirmada do escritor anterior e verificação da candidata. Uma
parada explícita impede a recuperação, inclusive quando não há publicação
ativa após uma falha na troca dos dados.

Verificações locais executadas:

- **331 testes Python aprovados**, incluindo circuito após três recuperações,
  partidas malsucedidas, configuração/conta alterada, cancelamento, parada
  durante espera, fila cheia, falta de memória na candidata, observação incerta,
  retomada por reserva, migração de apps existentes e proteção do formulário.
- O teste de concorrência usa duas conexões reais ao SQLite em arquivo,
  com as opções de produção (`IMMEDIATE`), e comprova uma única tentativa
  admitida para dois monitores concorrentes.
- `go vet ./...` e `go test -race ./...` aprovados. O executor confirma saída
  e OOM sem confundi-los com recurso estrangeiro ou falha de inspeção, também
  depois de recarregar seu estado; não inicia containers por conta própria.
- Ruff, formatação, consistência de migrações, `check --deploy` e
  `git diff --check` aprovados. O roteiro de navegador passou, incluindo
  habilitar recuperação e exibir a seção em 360 px. A primeira execução
  encontrou um seletor que não incluía o sufixo de rótulo do Django; o seletor
  foi corrigido e a execução completa passou.
- Pacotes construídos localmente. SHA-256 de `runtime5`:
  `6822095c318b5eb63f044fb0ef6b7063084731f5050e21c4cf51e6fed0891015`;
  de `control7`:
  `8141c5227d549be5b0b106d971d6eba14ca51be64cc24e7e4c7355f9fff0d941`.

Os bancos de desenvolvimento e da prévia receberam `0012` após backups SQLite
0600 em `var/backups/metadata-before-recovery-20261022T182157Z.sqlite3` e
`var/backups/preview-before-recovery-20261022T182157Z.sqlite3`. A prévia na porta
8010 foi reiniciada preservando contas, dados e chaves; `/healthz` respondeu
HTTP 200. Seu executor continua desabilitado.

O roteiro integrado foi ampliado para encerrar processos Shiny reais,
observar três recuperações, manter outra sessão reativa, reiniciar o worker
durante uma espera e depois do circuito aberto, e retomar manualmente pelo
navegador. **Esse novo ensaio com containers ainda não tem resultado registrado
neste marco.** Os pacotes estão prontos para o CI e para a homologação local
pelo operador. Os resultados anteriores de F3-05 não são evidência de execução
dos novos cenários.

O próximo item do backlog é **F3-07: backup/restauração básico**. O marco
combinado para teste manual completo ainda exige a homologação de F3-06,
F3-07 e uma instalação permanente de testes; não foi anunciado como pronto.

## Marco 33 — homologação local da recuperação de falhas (F3-06)

O relatório produzido pelo operador, iniciado em
`2026-10-22T18:36:52.205940+00:00`, foi conferido e preservado integralmente em
[recovery-runtime-local.json](evidencias/recovery-runtime-local.json).
Passaram **179 verificações integradas**, incluindo **123 da instalação e
navegador** e **13 da ativação**. Não há erro de execução ou limpeza. A API
repetiu uma remoção após HTTP 429, usando o mesmo ID
`8e52a290-9da1-524b-8226-c5501077a141`, e concluiu. Não houve repetição por
executor ocupado no ensaio de ativação. O teste do segundo escritor recebeu
`data_in_use` na primeira tentativa.

Os 22 novos critérios de recuperação passaram: habilitação pela interface,
três esperas seguidas de novas instâncias, preservação do dado persistente,
remoção dos escritores anteriores, fechamento das rotas antigas e outra
sessão Shiny ainda reativa. O worker foi reiniciado durante a segunda espera,
mantendo o contador. A quarta falha abriu o circuito; outro reinício do worker
e uma observação posterior confirmaram ausência de tentativa adicional.
A suspensão ficou visível, sem link para abrir o processo encerrado, e a
seção coube em 360 px. Desabilitar a política e reiniciar manualmente restaurou
o app com os dados preservados. Os cenários anteriores de publicação,
autenticação, segredos, rollback, drenagem e operações em lote também passaram.

Os processos foram encerrados explicitamente pelo roteiro com sinal KILL;
isso não é um ensaio de esgotamento real de RAM. As decisões de OOM e
observação incerta foram verificadas nos testes automatizados descritos no
marco 32. Este ensaio não mede capacidade, latência sob carga, reboot, emissão
ACME, DNS público ou QoS sob disputa.

Ambiente registrado: kernel `7.0.0-38-generic`, x86_64, 16 CPUs lógicas,
66.735.198.208 bytes de RAM, Podman 4.9.3, crun 1.14.1 e ext4. Os mapas de
usuários foram `0 655360 65536` e `0 720896 65536`. No teste de reinício de
painel/worker, os PIDs de auth, gateway e ingress permaneceram `2382778`,
`2382779` e `2382780`, respectivamente. O pacote `control7` instalado tem
SHA-256 `8141c5227d549be5b0b106d971d6eba14ca51be64cc24e7e4c7355f9fff0d941`,
igual ao artefato local. Os checksums de `runtime5` e `builder2` também foram
conferidos, e os três pacotes constam instalados.

A consulta posterior ao systemd confirmou painel, auth, worker, gateway,
ingress, manutenção, builder e runtime inativos, com resultado `success`.
Seus sockets e o timer de manutenção estavam inativos; `shinyd.target`,
desabilitado. O socket do agente de diagnóstico permaneceu ativo. A prévia
independente na porta 8010 respondeu HTTP 200 em `/healthz`. O ensaio não
deixou configurado atendimento permanente.

### CI: ensaio integrado aprovado, workflow cancelado

O [workflow do commit de implementação](https://github.com/rfsaldanha/shinyd/actions/runs/39831018892),
`87c73fbae13d7f0d14cd5143dc62d924948a970f`, terminou como
`completed/cancelled`. Python, Go e agente concluíram com sucesso. O job
isolado `runtime` foi cancelado, e sua anotação informa que excedeu o limite
de 20 minutos. Seus logs retornaram `NOT_FOUND`; não há relatório que permita
atribuir a causa a uma operação específica. Esse teste permanece sem conclusão
funcional comprovada nesta execução, e o workflow não é declarado aprovado.

Separadamente, o job builder produziu um
[relatório integrado aprovado](evidencias/recovery-runtime-ci.json), iniciado
em `2026-10-22T18:28:27.567748+00:00`, com **177 verificações**, incluindo
123 de instalação/navegador e 13 de ativação. Todas as etapas do job, inclusive
remoção do pacote preservando estado e envio de artefatos, constavam como
`completed/success`; seu estado agregado ainda aparecia `in_progress` na API.
Essa distinção foi preservada na evidência. Não houve erro de limpeza ou
repetições nesse relatório. As duas verificações adicionais do ensaio local
são a identidade e o checksum do runtime, instalado em etapa anterior no CI.

O runner integrado registrou kernel `6.17.0-1022-azure`, x86_64, quatro CPUs
lógicas e 16.765.378.560 bytes de RAM. Seu pacote `control7` tem SHA-256
`310b6118bbe66c00d0f8236615f6498b87e5f4798ee9f4f81654f323c9cdeef6`;
é uma construção distinta do artefato local, não identidade binária.

F3-06 está homologada neste host, com ensaio integrado remoto também aprovado
e a pendência do job isolado do executor registrada acima. O próximo item é
**F3-07: backup/restauração básico**. O teste manual completo combinado ainda
depende dessa etapa e da instalação permanente de testes. Esta rodada
preserva evidências e atualiza documentação; não altera código ou pacotes.

## Marco 34 — backup criptografado e restauração em vazio (F3-07)

Implementado em `control8`, sem novas migrações, mantendo `runtime5` e
`builder2`. O [guia de backup](BACKUP_RESTAURACAO.md) descreve a janela de
manutenção, armazenamento externo, tratamento das chaves e recuperação.

O operador pode criar, verificar e restaurar uma cópia com `shinyd-backup`.
O arquivo age contém banco consistente, fontes, artefatos, imagens OCI,
dados persistentes, configurações, certificado e chaves. A identidade age
fica fora da cópia. Links, arquivos especiais, excesso de tamanho e
referências ausentes são recusados. Apps, serviços e tarefas precisam estar
parados; não há desligamento automático de sessões pela ferramenta.

A restauração autentica todo o arquivo antes de interpretá-lo e exige uma
instalação vazia, com os mesmos pacotes, sistema e arquitetura. Confere IDs
das imagens carregadas, preserva dados e a chave dos segredos, troca chaves
de transporte, invalida acessos antigos e desabilita a recuperação automática.
Apps permanecem parados para conferência. Falha normal de instalação remove
apenas caminhos novos. Marcadores persistentes e 16 configurações systemd
impedem inicialização durante manutenção ou restauração incompleta. Queda
de energia, KILL e falha na própria limpeza não são tratados como transação
atômica; o guia orienta preservar o estado e restaurar em outra instalação vazia.

A página “Backup e restauração”, exclusiva de administradores, apresenta
contadores para preparação e o recibo da última restauração. Não recebe
arquivos, caminhos arbitrários ou chaves. O backup inicial é operado pelo
Linux; automação e execução pelo navegador permanecem para F6-01.

Verificações executadas:

* **361 testes Python aprovados**, incluindo 30 cenários novos de backup:
  age real, SQLite real, preservação de dados e segredos atuais/históricos,
  invalidação de acessos, chave errada, corrupção, truncamento, traversal,
  links, filas ativas, versões incompatíveis, espaço/limites, destino ocupado
  e limpeza de gravação parcial. O teste de orquestração simula os comandos
  Podman; não é evidência de transporte real de imagens.
* Ruff, ausência de novas migrações e `check --deploy` aprovados.
  `go vet ./...` e `go test -race ./...` aprovados, com resultados em cache
  para os pacotes Go, cujo código não mudou.
* Chrome local: nova página HTTP 200 em 1280 px e 360 px, sem overflow
  horizontal nem erros JavaScript. Capturas locais em
  `var/backup-panel-1280.png` e `var/backup-panel-360.png`.
* Pacote construído; três comandos executáveis, guia e 16 proteções systemd
  conferidos no conteúdo extraído. `systemd-analyze verify` terminou com
  código zero. A prévia da porta 8010 foi reiniciada com contas/chaves
  preservadas e executor desabilitado, sem alteração de esquema.

O artefato local `shinyd-control_0.1.0~control8_amd64.deb` tem SHA-256
`125a334ad0ca8938fabb188937c072773b7218d6fcca791006efe6a10d75bd6e`.
A dependência `age` foi obtida do repositório Ubuntu e extraída somente em
`var/age-toolchain` para os testes sem privilégios. Sua instalação no sistema
e a atualização do pacote de controle ainda precisam do operador; `sudo -n`
continua exigindo senha.

O roteiro `builder_smoke.py --activation --backup` foi preparado para repetir
o fluxo real, parar os apps pela interface, guardar a fixture original,
remover uma imagem publicada do cache, restaurar em vazio e verificar no
navegador dados, segredos e reatividade. Também verifica o bloqueio systemd
e as recusas de chave errada, truncamento, links e destino ocupado. O job
builder do CI passa a ter 45 minutos para incluir exportação, verificação
completa e restauração das imagens; isso não resolve nem reclassifica o
timeout anterior do job isolado runtime.

**A nova homologação com containers ainda não foi executada localmente.**
Não há evidência de aprovação desse novo fluxo no CI neste marco. F3-07 está
implementada e aguarda essa verificação. O momento combinado para teste
manual completo ainda exige sua homologação e a instalação permanente de
testes. Não foi declarado pronto para uso real ou para produção.

## Marco 35 — correção da preparação do ensaio de backup

O [relatório local](evidencias/backup-preparacao-local-falha.json), iniciado em
`2026-10-23T10:47:57.401863+00:00`, terminou com `passed: false` após cerca
de 22 minutos. Há 55 verificações aprovadas na lista principal e 126
registradas na seção instalada; a verificação agregada `installed_https_probe`
falhou. A limpeza terminou sem erro. Houve uma repetição de `ensure` após
HTTP 429, com o mesmo ID `462eecb1-b33b-45b4-b6a8-4deb1e668b8b`.

A falha ocorreu no programa enviado a `shinyd-admin`, antes da criação
do backup. O cenário anterior remove `APP_TOKEN` de propósito e comprova
`environment-unset` no R. A preparação do backup tentava buscar essa variável
com `.get()`, embora ela já não existisse. Isso foi reproduzido com ORM e
criptografia reais no teste de regressão. O roteiro agora cria seu próprio
segredo sintético, somente depois de confirmar que o app está parado,
preservando `APP_MESSAGE`, a versão publicada e os snapshots históricos.
A partida após restauração deverá comprovar essa configuração atual.

O [CI do commit d5ddf4a](https://github.com/rfsaldanha/shinyd/actions/runs/39841743751)
também falhou no mesmo ponto, com os mesmos arquivos/linhas na pilha.
Seu [relatório extraído dos logs](evidencias/backup-preparacao-ci-falha.json)
foi preservado com a identificação da origem. Python, Go, agente e o teste
isolado runtime passaram nessa execução; o job builder falhou.
O timeout do runtime na execução anterior não se repetiu nessa rodada.

O roteiro passa a mostrar etapas com horário no stderr: ambientes R, painel,
persistência/recuperação, criação, verificação e restauração do backup,
conferência no navegador e limpeza. O stdout continua reservado ao JSON final,
portanto `tee var/backup-report.json` permanece compatível. Novas falhas antes
de produzir uma cópia ou guardar originais removem os diretórios temporários
de preparação; cópias e originais continuam preservados em falhas posteriores.
O diretório antigo `/var/lib/shinyd-backup-probe-m0twvtlb`, registrado no
relatório recebido, não foi alterado nesta rodada.

Passaram **365 testes Python**, incluindo quatro verificações novas da
preparação com segredo removido/presente, recusa de app em execução e
separação de progresso e JSON. Ruff e formatação passaram. A mudança está
nos roteiros e testes; não altera o produto, as migrações ou os pacotes.
O `control8` instalado e o artefato usado têm SHA-256
`125a334ad0ca8938fabb188937c072773b7218d6fcca791006efe6a10d75bd6e`.
`age 1.1.1-1ubuntu0.24.04.3` está instalado, assim como `runtime5` e `builder2`.

A consulta pós-ensaio confirmou serviços, sockets e timer da pilha inativos,
com resultados `success`; a prévia na porta 8010 respondeu HTTP 200.
A partição que contém `/var/lib` tinha aproximadamente 8,1 GB livres, com
97% de ocupação. Esse espaço limitado **não causou a falha observada**,
mas pode impedir a exportação/cópia na tentativa seguinte. Não houve limpeza
de dados do host nem alteração de armazenamento para contornar os limites.

O novo roteiro ainda exige uma homologação completa. A execução recebida
não produziu backup nem demonstrou restauração. O marco combinado para
teste manual completo continua pendente da homologação de F3-07 e da
instalação permanente de testes.

## Marco 36 — diagnóstico da criação e limpeza após backup interrompido

O novo [relatório local](evidencias/backup-criacao-local-falha.json), iniciado
em `2026-10-23T11:22:25.893941+00:00`, passou pela preparação do segredo que
falhou antes. Também registrou a presença do link de teste, a recusa da
cópia contendo esse link e a preservação do alvo; o roteiro antigo não
registrava o motivo específico dessa recusa. A criação efetiva do backup
falhou em seguida, antes de registrar `backup_encrypted_archive_created`.
Há 55 verificações aprovadas na lista principal e 130 na seção instalada.
Nenhum backup ou restauração foi comprovado nesse ensaio.

A mensagem original do comando foi descartada pelo wrapper antigo, que
registrou apenas `RuntimeError` e a linha 116 de `backup_browser_probe.py`.
**A causa da criação ainda não está determinada.** A partição de `/var/lib`
tinha cerca de 7,4 GB livres após a execução, com 97% de ocupação; esse dado
isolado não comprova que a falta de espaço causou a falha.

O [CI de 9cc1a61](https://github.com/rfsaldanha/shinyd/actions/runs/39945333609)
falhou no mesmo ponto. O [relatório remoto](evidencias/backup-criacao-ci-falha.json)
registra os mesmos 130 checks instalados e 46 falhas na limpeza. Python, Go,
agente e runtime passaram; builder falhou. As mensagens de progresso mostram
o início da criação às 11:44:05 UTC e da limpeza às 11:45:16 UTC, sem preservar
a razão devolvida pelo comando. Não se atribui essa falha a uma causa por inferência.

A limpeza do ensaio local também falhou em 46 chamadas `remove`, porque o
backup havia parado explicitamente o socket do executor. O roteiro tentava
usar a API com esse socket desligado. Serviços e socket foram encontrados
inativos após a execução; não foram iniciados nesta rodada. As políticas,
registros e dados que ficaram após a limpeza incompleta precisam de inventário
antes de qualquer remoção ou nova homologação.

Correções nos roteiros:

* A limpeza confere a política que pertence à fixture e reabre o socket do
  executor antes de retirar as instâncias. Não inicia apps. Se houver marcador
  de manutenção/restauração incompleta, preserva o estado para recuperação.
* O painel de teste também preserva banco/configuração nesse caso. O relatório
  passa a incluir inventário de IDs, hashes das políticas e limites anteriores
  do grupo, para tornar uma limpeza incompleta verificável.
* Falhas de `create`, `verify` e `restore` passam a registrar operação,
  código de saída e uma categoria baseada em mensagens fixas da plataforma.
  Saídas desconhecidas, tracebacks e valores de segredos não são copiados.

Foi acrescentado `scripts/backup_failure_diagnose.py`, **somente de leitura**.
Ele compara os IDs remanescentes com o relatório, conta dados sem ler seu
conteúdo, verifica imagens locais por ID, consulta serviços/limites e informa
espaço disponível e uma estimativa conservadora baseada em imagens
descomprimidas. Não inicia ou remove serviços, containers, imagens ou arquivos,
e não lê as chaves. A estimativa não substitui medição do pico de armazenamento,
nem recupera a mensagem perdida da falha anterior.

```sh
cd /mnt/data/drive2/shinyd
set -o pipefail
sudo python3 scripts/backup_failure_diagnose.py --report var/backup-report.json \
  | tee var/backup-diagnostic.json
```

Passaram **374 testes Python**, incluindo proteção contra limpeza de uma
restauração interrompida, política diferente, socket indisponível, classificação
segura de erros e diagnóstico sem alterações nem exposição de segredos.
Ruff e formatação passaram. Não há alteração do produto ou dos pacotes;
o artefato `control8` continua com o checksum registrado no marco anterior.
O diagnóstico instalado requer sudo, que ainda exige senha nesta sessão.
F3-07 e a liberação do teste manual completo continuam pendentes.

## Marco 37 — Menor consumo de disco no backup e recuperação do ensaio

O diagnóstico executado pelo operador em **23/10/2026, 12:04 UTC** está em
[`backup-diagnostico-local.json`](evidencias/backup-diagnostico-local.json).
Todos os 15 serviços/acionadores consultados estavam inativos, sem containers
gerenciados, banco/configuração do painel ou marcadores de backup interrompido.
Restavam **43 registros de instâncias retiradas, 8 artefatos, 2 diretórios de
dados e as 2 políticas do ensaio**, todos associados ao relatório da falha.
Os arquivos de dados somavam 60 bytes. Nada foi removido pelo diagnóstico.

Havia **7.911.874.560 bytes livres (aproximadamente 7,4 GiB)** em `/var/lib`.
As dez imagens referenciadas somavam 17.459.486.325 bytes descomprimidos,
incluindo camadas repetidas. A estimativa conservadora do transporte antigo
era de 53.452.200.979 bytes; não mede o pico real e está incompleta porque o
banco já havia sido retirado pela limpeza. A mensagem original da falha local
continua indisponível; o diagnóstico comprova a restrição de espaço, não
recupera uma causa que o roteiro anterior deixou de registrar.

`control9` passa a exportar as imagens juntas por ID com `podman save
--multi-image-archive --format=docker-archive`, comprimindo a saída durante
a leitura. Camadas compartilhadas entram uma vez. O fluxo tem limite de
bytes descomprimidos, prazo, reserva de disco e tratamento de erros sem
reproduzir stderr. A cópia privada é liberada antes de cifrar e o TAR
decifrado antes de carregar imagens. Os temporários do Podman ficam na área
privada da operação. A leitura valida a expansão gzip e todos os IDs são
conferidos depois da carga. O formato novo é `shinyd-cold-backup-v2`; o leitor
mantém v1 e a restauração continua exigindo as versões exatas registradas.
Não há nova migração de banco nem mudança dos pacotes do executor/builder.

Experimento real com Podman **4.9.3 rootless**, usando três imagens locais e
armazenamento **vfs separado, inicialmente vazio** para a carga:

| Medida | Resultado observado |
| --- | ---: |
| Soma dos tamanhos individuais | 5.053.058.198 bytes |
| Arquivo conjunto sem compressão | 3.287.293.952 bytes |
| Arquivo conjunto comprimido | 1.222.312.301 bytes |
| Exportação comprimida | 40,61 s |
| Carga no armazenamento separado | 98,81 s |
| IDs conferidos e preservados | 3 de 3 |

A evidência é
[`backup-imagens-comprimidas-local.json`](evidencias/backup-imagens-comprimidas-local.json).
As imagens desse experimento são distintas das dez imagens rootful do ensaio
interrompido. Não se mediu pico de disco, carga de usuários ou backup instalado
completo; os números não são uma previsão para outro servidor.

A suíte completa passou com **395 testes Python**. Depois foram acrescentados
dois testes do marcador da quarentena; os **11 testes de recuperação da
fixture** passaram. Ruff e formatação passaram. Os cenários novos cobrem
processo com erro, timeout, limite de expansão, falta de disco, gzip truncado,
inventário divergente, manutenção interrompida, mudança de política, arquivos
desconhecidos e reversão das movimentações após falha de escrita.

Pacote construído, ainda **não instalado/homologado** neste marco:

* `shinyd-control_0.1.0~control9_amd64.deb`
* SHA-256: `104e01066f5acbc34b002910bee4a62754a2c55aaccbb47e2f93627dd8e2e8e4`

### Recuperar os resíduos identificados antes do próximo ensaio

`scripts/backup_fixture_recover.py` confere o hash do relatório, repete o
inventário, exige serviços/builds parados, ausência de containers, painel
vazio e nenhum marcador anterior. Recusa arquivos desconhecidos ou estado
diferente do diagnóstico. Com `--apply`, trava os componentes, bloqueia
inicializações e move somente os **55 caminhos identificados** para
`/var/lib/shinyd-probe-quarantine-*/originals`, com recibo privado e sem apagar
imagens. A operação usa renomeação no mesmo sistema de arquivos; não copia
os vários gigabytes das imagens. Os limites de memória observados ficam
preservados, pois o relatório antigo não registrou os valores anteriores.

```sh
cd /mnt/data/drive2/shinyd
set -o pipefail
sudo python3 scripts/backup_fixture_recover.py \
  --report var/backup-report.json \
  --diagnostic var/backup-diagnostic.json --apply \
  | tee var/backup-recovery.json
```

O comando sem `--apply` apenas confere e mostra a quantidade de caminhos.
Após sucesso, guarde a quarentena até a homologação: `receipt.json` registra
os caminhos originais relativos a `/` e os hashes dos relatórios usados.
Uma reversão manual deve manter serviços parados e exigir que cada destino
original esteja ausente antes de mover o correspondente arquivo de volta.
Não mescle essa fixture antiga com outra instalação. Falha normal tenta
reverter as movimentações já realizadas; interrupção abrupta ou estado parcial
preserva o marcador de manutenção e exige conferência do recibo pelo operador.

Essa operação ainda requer a senha sudo nesta sessão. **F3-07 e o teste manual
completo continuam pendentes** da recuperação do namespace, ensaio instalado
com `control9` e configuração de uma instalação permanente de testes.

## Marco 38 — Diagnóstico das recusas e preservação do estado local

O comando de recuperação foi executado em **23/10/2026, 13:46 UTC** e recusou
a operação com `remaining_control_or_jobs`. A evidência está em
[`backup-quarentena-local-recusa.json`](evidencias/backup-quarentena-local-recusa.json).
Essa recusa ocorre antes de criar a quarentena ou mover qualquer dado:
o painel ou o diretório de tarefas ainda contém entradas. O relatório antigo
não informava quais eram; sua identidade ainda precisa ser conferida.

Também foram conferidos os dois workflows completos:

| Commit / workflow | Resultado |
| --- | --- |
| `88ff5ab` / [39950504425](https://github.com/rfsaldanha/shinyd/actions/runs/39950504425) | Go, Python, executor e pacote do agente passaram; builder falhou em `create` |
| `82c3530` / [39953473415](https://github.com/rfsaldanha/shinyd/actions/runs/39953473415) | Go, Python, executor e pacote do agente passaram; builder falhou em `create` |

Em ambos, passaram **53 verificações principais** e foram registrados
**130 checks do ensaio instalado**, sem erro de limpeza. O agregado
`installed_https_probe` falhou. A correção que reabre o socket durante a
limpeza foi exercitada; a criação do backup ainda retornou
`unknown_failure`. Evidências:
[`backup-limpeza-ci-falha.json`](evidencias/backup-limpeza-ci-falha.json) e
[`backup-compressao-ci-falha.json`](evidencias/backup-compressao-ci-falha.json).
O experimento de compressão do marco anterior não identifica a causa dessas
recusas. Os resultados antigos não permitem recuperá-la retrospectivamente.

Foi corrigida a cobertura do diagnóstico do roteiro: além dos códigos já
conhecidos, ele aceita uma mensagem apenas quando ela corresponde exatamente
a um erro literal presente no código-fonte confiável de backup do repositório.
Nesse caso registra a mensagem e os locais em que ela é definida no código;
isso não representa uma pilha de execução observada. Saída desconhecida
continua omitida. Não interpreta conteúdo dos apps nem imprime stderr arbitrário.
Os cenários negativos passam a exigir a recusa específica esperada para
serviços ativos, links, chave incorreta, truncamento e destino ocupado.
Uma falha diferente já não conta como prova de que a proteção foi exercitada.

O diagnóstico de resíduos passou a informar tipos, tamanhos, donos e modos
das entradas do painel e das tarefas, sem ler seu conteúdo. Publica apenas
nomes fixos da plataforma e UUIDs válidos de tarefas; nomes desconhecidos
ficam como `unrecognized`. A recuperação mantém as mesmas restrições e passa
a incluir esses detalhes quando encontra a recusa observada.

Para obter somente esses metadados, sem exportar imagens nem repetir o
ensaio completo:

```sh
cd /mnt/data/drive2/shinyd
set -o pipefail
sudo python3 scripts/backup_failure_diagnose.py --namespaces-only \
  | tee var/backup-namespaces.json
```

Não há alteração do produto nem novo pacote: `control9` continua com o
checksum do marco 37. O roteiro e os testes de diagnóstico foram atualizados;
passaram **403 testes Python**, Ruff e formatação.
O CI voltará a executar a homologação com identificação mais completa das
recusas. A operação local ainda depende da senha sudo do operador.
**F3-07 e o teste manual completo continuam pendentes.**

## Marco 39 — Reconhecimento da trava do builder na recuperação

O diagnóstico de **23/10/2026, 14:02 UTC**, preservado em
[`backup-namespace-local.json`](evidencias/backup-namespace-local.json),
confirmou o diretório do painel vazio. No diretório de tarefas havia somente
um arquivo regular de **0 bytes**, dono/grupo **root:root**, modo **0600** e
um vínculo. O diagnóstico anterior ocultava o nome como `unrecognized`.

A inspeção de `backend/builder/api.py` encontrou a trava `.api-lock`, criada
na pasta de tarefas e mantida ali quando a API encerra. O roteiro exigia essa
pasta completamente vazia, portanto recusava também essa estrutura normal.
Os metadados observados são compatíveis com a trava; a próxima execução
conferirá o nome efetivo, sem autorizar arquivos arbitrários pela semelhança.

A recuperação passa a aceitar exclusivamente `.api-lock` regular, vazia,
com um vínculo, modo 0600 e dono/grupo do operador root. Abre sem seguir links
e tenta adquirir sua trava exclusiva sem aguardar. Se estiver em uso, recusa
a operação. Mantém o descritor aberto durante toda a quarentena e reconfere
o arquivo pelo dispositivo e inode antes de mover os resíduos. O arquivo de
trava fica no local, com conteúdo e identidade preservados; não entra na lista
de dados movidos. O resultado informa `preserved_builder_api_lock`.

O painel continua precisando estar vazio, e arquivos desconhecidos, tarefas
restantes, serviços ativos e mudanças no inventário continuam impedindo a
operação. O diagnóstico passou a reconhecer o nome fixo `.api-lock` sem ler
seu conteúdo nem revelar nomes desconhecidos.

Passaram **37 testes específicos** de diagnóstico e recuperação. Os casos
novos usam locks reais e verificam preservação do arquivo, liberação depois
da operação, recusa de trava em uso, conteúdo, permissões/donos diferentes,
links, FIFO, diretório e substituição do arquivo após abrir o descritor.
A suíte completa passou com **414 testes Python**; Ruff e formatação passaram.

Não há alteração do produto nem dos pacotes; `control9` mantém o checksum
registrado no marco 37. A recuperação instalada ainda depende da senha sudo:

```sh
cd /mnt/data/drive2/shinyd
set -o pipefail
sudo python3 scripts/backup_fixture_recover.py \
  --report var/backup-report.json \
  --diagnostic var/backup-diagnostic.json --apply \
  | tee var/backup-recovery.json
```

Este comando aplica a quarentena somente se todas as conferências passarem;
não repete a publicação pelo navegador. **F3-07 e o teste manual completo
continuam pendentes.**

## Marco 40 — Quarentena concluída e correção do backup com SQLite WAL

A recuperação local foi concluída em **23/10/2026, 15:30 UTC**. O relatório
[`backup-quarentena-local-sucesso.json`](evidencias/backup-quarentena-local-sucesso.json)
registra `passed: true`, `applied: true` e os **55 caminhos** movidos para
`/var/lib/shinyd-probe-quarantine-j437nwoh`. A trava `.api-lock` permaneceu no
local. Nenhuma imagem foi removida e os limites de recursos não foram alterados.
Mantenha essa quarentena até a conclusão da homologação.

O [workflow de `d08edda`](https://github.com/rfsaldanha/shinyd/actions/runs/39967368155)
aprovou Go, Python, executor e pacote do agente. O builder falhou no cenário
negativo `backup_unsafe_link_refused`: o backup recusou o link, mas o roteiro
esperava `unsafe_entry` em vez de `inaccessible_entry`. Há **53 verificações
principais aprovadas**, uma falha agregada e **128 checks** registrados no ensaio
instalado, sem erro de limpeza. A evidência está em
[`backup-link-ci-falha.json`](evidencias/backup-link-ci-falha.json).
O roteiro agora exige a classificação efetivamente emitida ao abrir um link
com `O_NOFOLLOW`; um teste usa a rotina real de cópia e verifica que o destino
do link não é copiado nem alterado.

Também foi reproduzida uma falha do produto usando o modo **WAL** do SQLite,
ativado pelo painel. Nesse modo, alterações confirmadas podem permanecer em
um arquivo auxiliar antes de serem incorporadas ao arquivo principal. A cópia
herdava esse modo e sua inspeção criava arquivos `-wal` e `-shm`, recusados pelo
inventário restrito do backup. O cenário com WAL falhou em `archive.pack` com
“Conteúdo fora da estrutura permitida para backup.”; o cenário com DELETE passou.
Isso comprova uma regressão local; não recupera a mensagem perdida nos ensaios
antigos nem demonstra retrospectivamente a causa de cada falha anterior.

O `control10` usa a API de backup do SQLite e consolida **somente a cópia
temporária** em modo DELETE antes de validá-la. Fecha explicitamente as conexões
de cópia, inspeção e redefinição de acesso. O contexto de conexão do Python
gerencia a transação, mas não fecha a conexão, conforme a
[documentação oficial](https://docs.python.org/3/library/sqlite3.html#how-to-use-the-connection-context-manager).
O banco de origem mantém WAL. Na restauração, os serviços voltam a ativá-lo.

Os dois cenários passaram após a correção, com `age` e SQLite reais. O teste
mantém a origem aberta, desativa o checkpoint automático, confirma uma alteração
presente apenas no WAL e a recupera após criar, verificar e restaurar o backup.
Dados e chave dos segredos permanecem preservados. A evidência
[`backup-wal-regressao-local.json`](evidencias/backup-wal-regressao-local.json)
explicita que Podman e os privilégios de instalação são simulados nesse teste;
ele não substitui o ensaio instalado. Outros testes verificam fechamento das
conexões tanto após commit quanto após rollback da transação.

A suíte completa passou com **418 testes Python**. Foi corrigida também uma
instabilidade preexistente no teste de upload idempotente: a repetição agora
reutiliza os mesmos bytes do ZIP, incluindo seus horários, em vez de gerar
outro arquivo. A regra do produto que recusa conteúdo diferente com a mesma
chave permanece inalterada. Ruff, formatação, migrações em banco temporário,
ausência de migrações novas e verificações Django de implantação passaram.

Pacote construído e conferido, ainda **não instalado/homologado** neste marco:

* `shinyd-control_0.1.0~control10_amd64.deb`
* SHA-256: `762394953a30ac03fd33195d4d7b54a5e2ff5bdbe7504982e51415e98bf79d62`

Não há nova migração, nem alteração dos pacotes `builder2` e `runtime5`.
Com os resíduos recuperados e o pacote corrigido disponível, o próximo passo
é repetir o ensaio instalado completo. Ele instala os pacotes, publica apps
de teste e exercita backup/restauração. Pode levar mais de 20 minutos e informa
progresso no terminal. A execução local continua dependendo da senha sudo do
operador, indisponível para esta sessão do agente:

```sh
cd /mnt/data/drive2/shinyd
set -o pipefail
sudo .venv/bin/python scripts/builder_smoke.py --activation --backup \
  --install-package dist/shinyd-builder_0.1.0~builder2_amd64.deb \
  --install-runtime-package dist/shinyd-runtime_0.1.0~runtime5_amd64.deb \
  --install-control-package dist/shinyd-control_0.1.0~control10_amd64.deb \
  | tee var/backup-report.json
```

**F3-07 continua com homologação instalada pendente.** O teste manual completo
combinado depende dessa aprovação e da configuração de uma instalação
permanente de testes; ainda não foi liberado.

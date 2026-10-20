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

Os **163 testes Python** passaram, incluindo transporte Unix real com recusa de
UID indevido, requisições inválidas, concorrência, retomada, upload interrompido,
cancelamento, conflito de leases e revogação de permissão. Ruff e verificações
Django/migrações passaram. O pacote foi gerado e as novas unidades foram
verificadas pelo systemd; a verificação também apontou o `MemoryMax=0` do pacote
antigo do executor ainda instalado neste host, já corrigido no código no marco 12.

O roteiro privilegiado foi ampliado para testar views e worker Django contra a
API instalada, com a identidade real `shinyd-control` e banco descartável.
Sua execução no CI e a homologação local deste novo pacote ainda estão pendentes.

Este marco cobre a integração de F2-03 e parte de F2-06. **Ainda não ativa
publicações**: o próximo contrato deve permitir ao executor reconhecer um
artefato preparado, iniciar a candidata e registrar sua rota após readiness,
preservando a publicação anterior em caso de falha. Downloads, retenção visual,
prioridades da fila e homologação de reboot continuam pendentes.
Contrato, limites e demonstração: [BUILDER_PAINEL.md](BUILDER_PAINEL.md).

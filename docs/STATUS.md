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
* Roteiro privilegiado e job de CI preparados para instalar, verificar
  restrições/recuperação e remover o pacote preservando estado em máquina
  descartável. Resultado dessa homologação ainda pendente neste registro.

Contrato, instalação e limites: [AGENTE_EXECUCAO.md](AGENTE_EXECUCAO.md).
Evidência sem privilégios: [agente-diagnostico.json](evidencias/agente-diagnostico.json).
É a primeira parte de F1-03/F1-05; criação/parada de containers, rede, instalação
do controle completo, registro no gateway e reboot continuam pendentes.

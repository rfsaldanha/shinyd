# Executor de containers — contrato inicial

O novo componente `shinyd-runtime` cria, consulta, para e remove instâncias
mediante operações tipadas. É parte de F1-05/F2-04/F2-07. O pacote `runtime2`
acrescenta reconhecimento dos artefatos do builder e encerramento definitivo
de candidatas. Drenagem e instalação completa do painel continuam pendentes.

## Isolamento e atendimento

O container usa Podman rootful, `userns=auto`, UID/GID 65534, raiz somente
leitura, capacidades removidas, seccomp, no-new-privileges e AppArmor imposto.
Não há volumes do host nem portas publicadas. Volumes declarados pela imagem
são ignorados. `/tmp` é um tmpfs de 64 MiB. O processo precisa atender na porta
3838; o catálogo é de imagens locais imutáveis `sha256:<ID>`, previamente
aprovadas pelo operador. A API não recebe comandos, caminhos, imagens, volumes
ou variáveis arbitrários e nunca faz pull.

Cada instância usa `--network=none`, com somente loopback próprio. O atendimento
passa por `/run/shinyd-runtime/apps/<UUID>.sock`, dono root, grupo
`shinyd-gateway`, modo 0660. Um relay abre sockets TCP no namespace de rede
da instância e os entrega ao encaminhamento normal do Go. O namespace é
fixado por descritor, após verificar identidade/cgroup, remapeamento, restrições
e AppArmor do processo. PID reutilizado não é um destino de conexão.

O gateway aceita somente o caminho canônico vinculado ao ID da instância e
verifica UID root do listener. HTTP, WebSocket, uploads e downloads passam pelo
mesmo canal. A autenticação central permanece no gateway; o socket de controle
não é montado no app.

**Limitação deliberada:** apps não acessam internet, bancos externos, host ou
outros apps. Exceções de saída exigem uma política e testes próprios antes de
serem oferecidas. Este caminho não depende de regras de firewall globais.

Fontes primárias: [Podman 4.9.3](https://docs.podman.io/en/v4.9.3/markdown/podman-run.1.html),
[setns](https://man7.org/linux/man-pages/man2/setns.2.html) e
[runtime.LockOSThread](https://pkg.go.dev/runtime#LockOSThread).
O código usa `golang.org/x/sys/unix`. A thread é restaurada antes de entregar
o socket ao Go; falha na restauração encerra a thread ainda bloqueada.
Na verificação de AppArmor, o perfil padrão esperado do Podman precisa integrar
o rótulo real do processo e aparecer em modo `enforce` na política carregada em
`securityfs`. Um rótulo `mixed` só é aceito com essas duas provas; esse rótulo
pode representar o perfil imposto do container combinado ao perfil do `crun`.
Referência: [empilhamento de perfis AppArmor](https://www.apparmor.net/advanced/AppArmorStacking/).

## Privilégios e persistência

O agente de diagnóstico conserva seu serviço e isolamento anteriores.
O executor é um serviço separado, root, com API privada autorizada pelo UID
`shinyd-control`. Ele precisa administrar Podman, namespaces e cgroups;
comprometer esse serviço equivale a comprometer um componente privilegiado.
O catálogo e o estado são controlados por root, sem escrita por publicadores.
O catálogo de receitas permanece sob controle do operador. A opção explícita
de artefatos do builder está descrita ao final deste documento.
O serviço do executor permite a preparação privilegiada dos mapeamentos de
UID pelo Podman; `no-new-privileges` é aplicado aos apps e conferido no processo
real. Não se aplica essa restrição ao processo que prepara o armazenamento.
Eventos próprios do Podman ficam desativados, evitando rótulos de imagens na
auditoria do serviço.

Estado fica em `/var/lib/shinyd-runtime`, com escrita temporária, fsync,
rename e fsync do diretório. Um lock impede dois executores sobre o mesmo estado.
O registro precede a criação. Nome determinístico, imagem e labels vinculados
ao registro impedem duplicação e remoção de containers alheios. Conflito retorna
409. Repetir ensure com os mesmos dados é idempotente; alterar a especificação
do mesmo UUID é recusado.

Ao reiniciar, o executor reconecta instâncias já em execução. Instâncias
encerradas ficam em falha e exigem nova solicitação explícita. Não há loop de
reinício; até três tentativas de partida por UUID em dez minutos.
Parada é imediata e encerra conexões daquela instância. **Reiniciar o executor
encerra todas as conexões dos relays**, embora os containers permaneçam; exige
janela de manutenção. Sessões R não migram. Drenagem e retomada após reboot
continuam pendentes.

## Recursos

Hierarquia inicial: `shinyd/control` e `shinyd/workloads/interactive`;
grupos high/normal/low com pesos 400/200/100, app com peso padrão igual dentro
do grupo e containers abaixo do app. Não há quota de CPU nem teto de RAM
individual por app. A disputa real entre grupos ainda exige teste de carga.

O orçamento de memória é agregado. `memory_budget_mib: 0` calcula RAM menos
max(1 GiB, 15% da RAM); valor explícito não pode consumir essa reserva.
MemoryHigh fica em 90% do orçamento; swap não aumenta o orçamento.
Partidas reservam estimativa conservadora de 512 MiB por instância desejada,
exigem memória disponível além da reserva e PSI full avg10 até 1%.
`max_instances` limita partidas no servidor, não usuários nem capacidade.
Não se promete latência ou número de sessões.

Uma mutação ocorre por vez; concorrência retorna 429 com Retry-After, sem
fila oculta. Há 128 conexões simultâneas nos relays e prazos de controle.
Monitoramento de falha do processo ocorre a cada dois segundos.
Encerramento de ociosas, tratamento avançado de pressão, métricas de sessões e
admissão por picos observados ainda pertencem à fase de QoS.

## Pacote e demonstração

```sh
python3 scripts/build_agent_package.py --component runtime
go build -o dist/shinyd-proxy-probe ./cmd/shinyd-proxy-probe
```

O pacote instala serviços desabilitados. Não cria catálogo nem habilita execução.
O roteiro `scripts/runtime_smoke.py` exige root e uma instalação sem instâncias
ou política existentes. Instala o pacote quando solicitado, prepara somente
uma fixture Shiny confiável, cria duas instâncias pela API e verifica acesso,
isolamento, idempotência, recuperação e parada isolada.
O navegador roda como o usuário que invocou sudo. Ao terminar, remove somente
as instâncias e a política do teste, para o executor e mantém imagens em cache.
Não altera a autorização do agente de diagnóstico nem reinicia o host.

```sh
sudo .venv/bin/python scripts/runtime_smoke.py \
  --install-package 'dist/shinyd-runtime_0.1.0~runtime2_amd64.deb' \
  | tee var/runtime-report.json
```

Requer agente já instalado, Podman/rootful homologado, reserva de subuids,
Playwright e Chrome. A fixture requer baixar a imagem R fixada em
`experiments/r-images.json`. Não é o fluxo de publicação comum do produto.
O roteiro passou nas 25 verificações do
[CI Ubuntu 24.04](https://github.com/rfsaldanha/shinyd/actions/runs/39440034585).
O [relatório](evidencias/executor-ci.json) registra o ambiente observado e
as verificações posteriores de preservação do agente de diagnóstico.

Em 20/10/2026, as mesmas 25 verificações passaram no host local, incluindo
preservação do ID do container e do PID no host após reiniciar o executor.
O [relatório local](evidencias/executor-local.json) registra hardware, versões,
AppArmor imposto e checksum do pacote. A conferência posterior confirmou o
executor parado, seu socket desabilitado e o agente de diagnóstico ativo.
Essa homologação funcional não cobre reboot, publicação de ZIPs ou capacidade
sob carga.

## API e manutenção

* `PUT /v1/instances/<UUID>`: JSON exato `app_id`, `recipe`, `priority`.
* `GET /v1/instances/<UUID>`: estado, especificação e destino somente se pronto.
* `POST /v1/instances/<UUID>/stop`: parada imediata, sem corpo.
* `DELETE /v1/instances/<UUID>`: remove somente container/registro próprios.

Chamadas usam `Host: runtime` e socket `/run/shinyd-runtime/control.sock`.
`shinyd-runtimectl ensure UUID` lê JSON pela entrada padrão. Não há API pública
para cadastrar imagens; o futuro builder fornecerá o catálogo de artefatos
verificados. Auditoria contém operação, UUID, UID e código HTTP, nunca corpo,
argumentos de Podman ou saída do app. Logs do container ficam restritos no
armazenamento root do Podman, com rotação de 1 MiB; não integram exportações.

Remoção do pacote preserva política, registros e containers. Remova instâncias
pela API antes da desinstalação para liberar esses recursos. Isso não apaga
dados de outros componentes nem oferece purge de dados.

## Artefatos preparados e encerramento definitivo

A política aceita `builder_artifacts: true`, desabilitado por padrão. Nessa
modalidade `recipes` pode ser vazio. O pedido de partida contém `app_id`,
`priority`, `recipe: ""`, `artifact` (chave de 64 dígitos hexadecimais) e
`source_hash`; não aceita referência de imagem ou caminho.

O executor lê `/var/lib/shinyd-builder/artifacts/<chave>.json`, exigindo root
como proprietário, ancestrais protegidos, arquivo regular sem links e tamanho
limitado. Confere esquema, app, fonte, chave, digest e os rótulos de selagem da
imagem local. O manifesto é fornecido pelo builder, não pelo painel.
A partida conserva as proteções de processo, rede e recursos já existentes.

Para artefatos, repetir a partida retorna o estado da tentativa original.
Instância que falhou ou parou exige nova publicação com outro UUID. A rota só
é informada após o teste HTTP de funcionamento e as verificações de isolamento.
O pedido `POST /v1/instances/<UUID>/retire`, com a mesma especificação JSON,
persiste o encerramento definitivo antes de remover o container. Funciona
também antes da primeira partida. Um PUT atrasado fica bloqueado após a
confirmação, inclusive se o executor reiniciar.

Esses registros de encerramento são mantidos no estado do executor e contam no
limite inicial de 256 registros; sua coleta segura ainda não foi implementada.
Não os remova enquanto workers puderem conservar pedidos antigos. Falha de
remoção fica visível e permite repetir `retire`, sem relançar a instância.
A API de receitas anterior permanece compatível, incluindo a exclusão de
registros de suas fixtures.

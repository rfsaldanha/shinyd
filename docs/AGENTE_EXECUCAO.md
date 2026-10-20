# Agente de execução: primeira capacidade

Este marco entrega **diagnóstico somente de leitura**, instalação do agente
como serviço e a página administrativa “Diagnóstico do servidor”. Não conclui
F1-03/F1-05: a instalação do painel/worker/gateway, registro automático de
instâncias e reboot ainda exigem entregas e homologação. Operações de containers
e rede de atendimento estão no componente separado
[shinyd-runtime](EXECUTOR_CONTAINERS.md). Uploads continuam sem execução.

A instalação e os reinícios do serviço passaram no
[CI Ubuntu 24.04](https://github.com/rfsaldanha/shinyd/actions/runs/39430925787),
incluindo remoção que preserva estado. A instalação local pelo operador também
passou nas **15 verificações** em 20/10/2026:
[relatório do serviço local](evidencias/agente-servico-local.json).
Serviço e socket foram confirmados ativos; socket habilitado para iniciar com
o sistema, com dono `root:shinyd-control` e permissão `0660`.
Linux mínimo sem ferramentas adicionais e recuperação após reboot continuam
pendentes. O relatório original em `var/agent-service-report.json` conserva o
`boot_id` necessário ao teste posterior de reboot; essa identificação foi omitida
da cópia pública.

## Contrato e fronteira de confiança

`shinyd-agent` recebe um único socket Unix do systemd. Não escuta TCP e não
substitui arquivos de socket. A única operação é `GET /v1/status`, sem corpo ou
parâmetros, com `Host: agent`. Versão do contrato: 1. A resposta distingue
pré-requisitos observados de capacidade para executar apps.

O socket tem dono `root:shinyd-control`, permissão `0660`, diretório `0755`.
O agente consulta `SO_PEERCRED` em cada conexão e permite apenas o UID da conta
`shinyd-control`, resolvido ao iniciar. Pertencer ao grupo permite alcançar o
socket, mas não substitui a verificação de UID. Nem root é um chamador autorizado
implicitamente; root consegue administrar o sistema e não é uma fronteira contra
um administrador malicioso. A conta é criada bloqueada, com shell `nologin`.

Clientes verificam o UID do criador do listener (root/PID 1 na instalação).
Não há segredo compartilhado para esta operação. Cabeçalhos não definem
identidade. Sem proxy HTTP, redirecionamentos ou fallback para TCP. Respostas
maiores que 64 KiB, incompatíveis ou com mais de 60 segundos são recusadas.
Somente administradores do painel consultam essa informação. O caminho do
socket vem da configuração local, nunca de parâmetros de navegador.

O agente não invoca Podman, shell ou subprocessos; usa o diagnóstico já existente
do projeto. Observações são compartilhadas por até 10 segundos entre chamadas.
Há no máximo 32 conexões aceitas, backlog 32 e prazos para cabeçalhos/leitura.
Auditoria no journal contém somente operação conhecida, UID e resultado HTTP,
sem URL, argumentos, cabeçalhos ou conteúdo. Rate limit do journal pode suprimir
eventos sob excesso de consultas; não é ainda a auditoria durável de mutações.

O serviço roda como root com capacidades removidas, sistema de arquivos somente
leitura, sem acesso à rede do host e com acesso somente a sockets Unix. Limites
de 128 MiB/64 tarefas protegem este pequeno serviço de controle; não são cotas
de apps. Reinícios após falha usam espera de 5 segundos, no máximo 3 partidas em
120 segundos. Atingir o limite exige diagnóstico e `reset-failed` do operador.
Essas restrições são da capacidade de diagnóstico: não devem ser relaxadas
indiscriminadamente quando operações de runtime forem adicionadas.

Fontes primárias consultadas: [credenciais de sockets Unix](https://man7.org/linux/man-pages/man7/unix.7.html),
[ativação por socket](https://www.freedesktop.org/software/systemd/man/latest/systemd.socket.html)
e [isolamento de serviços](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html).
Compatibilidade das unidades deve ser conferida também com o systemd 255 do
Ubuntu 24.04; documentação da versão mais recente não é evidência de execução.

## Build e teste sem privilégios

```sh
go build -o dist/shinyd-agent ./cmd/shinyd-agent
go build -o dist/shinyd-agentctl ./cmd/shinyd-agentctl
python3 scripts/agent_probe.py > var/agent-probe.json
python3 scripts/build_agent_package.py
```

O probe herda um socket real, consulta o diagnóstico, recusa argumentos, encerra
e reinicia o processo mantendo o socket, verifica negação de UID e a auditoria.
Não equivale a instalar o serviço de sistema. O pacote e seu SHA-256 ficam em
`dist/`; o build não exige root nem altera serviços. O checksum verifica
integridade local, não oferece assinatura de um distribuidor.

## Instalação e homologação do serviço

Referência: Ubuntu 24.04 amd64, systemd como PID 1. O pacote instala somente o
agente/cliente e suas unidades, cria a conta bloqueada e `/var/lib/shinyd` se
necessário. Não instala dependências R, não modifica subuids, não altera sudoers
e não copia nem substitui `var/` do ambiente de desenvolvimento.

Revise o pacote, os scripts em `packaging/debian` e o roteiro abaixo. Na raiz
do repositório, o operador pode executar:

```sh
sudo python3 scripts/agent_service_smoke.py \
  --install-package 'dist/shinyd-agent_0.1.0~agent1_amd64.deb' \
  | tee var/agent-service-report.json
```

O roteiro instala o pacote local, confirma permissões/identidades, consulta o
agente como `shinyd-control`, verifica negação de outro UID, restrições efetivas
do processo e recuperação após reinício do agente e recriação do socket.
O pacote permanece instalado e habilitado. Não reinicia a máquina nem toca em
containers, apps, banco ou chaves. Não declara reboot aprovado.
Após verificar o serviço ativo, o roteiro zera a contagem de partidas desse
serviço antes dos dois reinícios controlados, permitindo repetir a homologação.

Sem instalação, para repetir a homologação do pacote já instalado:

```sh
sudo python3 scripts/agent_service_smoke.py | tee var/agent-service-report.json
```

Depois de um reboot autorizado e realizado pelo operador, repetir o roteiro com
`--after-reboot-from ID` e o `boot_id` do relatório anterior comprova que houve
mudança de boot, além de repetir as verificações. Não reinicie uma máquina
compartilhada apenas para este teste sem uma janela apropriada.

```sh
sudo python3 scripts/agent_service_smoke.py --after-reboot-from ID_DO_RELATORIO_ANTERIOR
```

## Integração com o painel

O painel usa `/run/shinyd-agent/control.sock` por padrão; `SHINYD_AGENT_SOCKET`
permite definir outro caminho local. Na instalação futura, painel e worker
executarão sob a identidade de controle; o gateway terá identidade separada.
Neste marco eles ainda são processos de desenvolvimento. Um painel iniciado
como o desenvolvedor **não é autorizado** pelo agente de sistema.

Em experimentos sem root, `SHINYD_DEV=1` permite definir
`SHINYD_AGENT_DEV_UID` para o UID real do listener de teste. Produção ignora
essa opção e exige root. Não adicione o desenvolvedor a grupos nem troque o
usuário autorizado do serviço para contornar essa separação.

Quando o agente está ausente, a página explica a indisponibilidade; o catálogo
continua funcionando. Não exibe diagnósticos antigos como se fossem atuais.

## Manutenção e remoção

```sh
sudo -u shinyd-control shinyd-agentctl status
sudo journalctl -u shinyd-agent.service
sudo systemctl status shinyd-agent.socket shinyd-agent.service
```

Uma atualização do pacote reinicia apenas o agente, preserva a escolha de
desabilitar o socket e mantém arquivos de dados/chaves. Não há migração de banco
neste pacote. O controle de versão/rollback do produto completo continua pendente.

```sh
sudo apt remove shinyd-agent
```

Remoção e purge param serviço/socket e preservam `/var/lib/shinyd`, a conta
bloqueada e dados de outros componentes. Não há operação de apagamento de dados
nesse pacote. A conta permanece para evitar reaproveitamento de UID enquanto
existirem arquivos que lhe pertençam.

## Continuação em componente separado

O [executor de containers](EXECUTOR_CONTAINERS.md) implementa criação,
consulta e parada idempotentes com imagens locais imutáveis, identificadores
internos e perfis aprovados. Tem pacote e serviço próprios; as restrições do
diagnóstico permanecem. Preparação de ambientes e conexão ao fluxo de publicação
continuam pendentes.

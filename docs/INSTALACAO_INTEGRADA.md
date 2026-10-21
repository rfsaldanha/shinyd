# Instalação integrada experimental

O pacote `shinyd-control 0.1.0~control4` reúne painel, worker, autorizador,
gateway e entrada HTTPS. Plataforma de referência: Ubuntu 24.04 amd64,
Python 3.12, systemd, cgroups v2 e os pacotes de agente, executor `runtime4`
e builder `builder2`. Não constitui declaração de prontidão para produção.
As evidências executadas e os limites estão em [STATUS.md](STATUS.md).

## Serviços e fronteiras

Caddy recebe HTTPS; arquivos estáticos pertencem ao pacote. O painel recebe
HTTP somente por socket Unix privado. O autorizador tem outro socket, acessível
apenas pelo gateway. A API privada não integra as rotas públicas. O gateway
acessa os sockets das instâncias mantidos pelo executor.

Painel, worker e autorizador usam `shinyd-control`, pois compartilham o banco.
O gateway usa `shinyd-gateway`; Caddy usa `shinyd-ingress`. Nenhum executa como
root. Somente Caddy recebe capacidade para abrir porta baixa. O executor e o
builder mantêm seus contratos privilegiados separados. Serviços de controle
usam limites de memória, reinício com intervalo e limite de tentativas;
apps continuam na hierarquia de workloads existente.

O systemd cria os sockets com grupo específico e modo 0660. Gunicorn herda o
descritor, mantendo umask 0077 para banco, uploads e chaves. O gateway instalado
não abre TCP. Caddy sobrescreve o protocolo encaminhado e é o único ponto de
entrada. Logs de acesso HTTP estão desabilitados; não se registram corpos,
cookies ou cabeçalhos de autenticação. Falhas operacionais aparecem no journal.

A escolha usa suporte documentado de Caddy a
[WebSockets e sockets Unix](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)
e a [certificado/chave em arquivo](https://caddyserver.com/docs/caddyfile/directives/tls),
e o suporte de [Gunicorn a descritores herdados](https://docs.gunicorn.org/en/stable/settings.html#bind).
A homologação é necessária além da configuração: ela testa o transporte real.

## Preparar e instalar

Gere os pacotes no checkout revisado. O pacote de controle incorpora wheels
binários das dependências operacionais fixadas em `requirements.lock`;
`dependency-hashes.json` registra seus hashes. A construção baixa dependências,
mas a instalação do `.deb` não executa pip nem baixa código Python.

```sh
python3 scripts/build_agent_package.py
python3 scripts/build_agent_package.py --component runtime
python3 scripts/build_builder_package.py
.venv/bin/python scripts/build_control_package.py
sudo apt-get --no-remove install podman crun uidmap slirp4netns fuse-overlayfs caddy
sudo dpkg --install dist/shinyd-agent_0.1.0~agent1_amd64.deb \
  dist/shinyd-runtime_0.1.0~runtime4_amd64.deb \
  dist/shinyd-builder_0.1.0~builder2_amd64.deb \
  dist/shinyd-control_0.1.0~control4_amd64.deb
```

O pacote não inicia o painel automaticamente antes da configuração. Não modifica
a configuração nem desabilita um Caddy existente. Verifique a disponibilidade
da porta escolhida. Configure previamente subuids, catálogo de imagens aprovado
e políticas conforme [EXECUTOR_CONTAINERS.md](EXECUTOR_CONTAINERS.md) e
[BUILDER_OFFLINE.md](BUILDER_OFFLINE.md). Ativação exige `builder_artifacts: true`.
O assistente de catálogo e a instalação em um único comando continuam pendentes.

Prepare DNS para `painel.exemplo.org` e `*.apps.exemplo.org`, apontando ao servidor,
e certificado PEM com cadeia que cubra ambos os nomes. Neste marco, forneça o
certificado e sua chave sem senha; emissão e renovação automáticas não estão
implementadas. A configuração valida validade, correspondência da chave e SANs.
A confiança da cadeia pública deve ser conferida pelo operador e pelo navegador.

```sh
sudo shinyd-configure --panel-host painel.exemplo.org \
  --app-domain apps.exemplo.org \
  --certificate /caminho/fullchain.pem --private-key /caminho/privkey.pem
sudo shinyd-admin createsuperuser
sudo systemctl enable --now shinyd.target
```

Abra `https://painel.exemplo.org`. Não há senha padrão. O comando administrativo
descarta variáveis de desenvolvimento herdadas e usa a identidade de controle.
A emissão de recuperação de conta continua disponível em `shinyd-admin recoveradmin`.
O pacote serve somente HTTPS; redirecionamento da porta 80 não faz parte deste marco.
`--bind` e `--https-port` permitem escolher IP IPv4 e porta; o padrão é
`0.0.0.0:443`. O domínio de apps deve ser separado do host do painel.

## Estado, atualização e remoção

Dados e chaves ficam em `/var/lib/shinyd-control`, com acesso exclusivo da conta
de controle. É separado do estado de desenvolvimento em `var/` e do diretório
de diagnóstico `/var/lib/shinyd`. O novo pacote não importa esses bancos.
Configuração fica em `/etc/shinyd/control.json`; cópia da chave do gateway em
`/etc/shinyd-gateway/key`; certificado, chave TLS e Caddyfile em `/etc/shinyd-tls`.
Esses arquivos são legíveis somente pelos grupos necessários.

`shinyd-configure` não altera nomes de uma instalação existente. Repeti-lo com
os mesmos parâmetros e serviços parados preserva chaves e cria backup SQLite
antes de migrar metadados. Não é backup completo: arquive também chaves,
configurações, fontes e estados do builder/runtime antes de atualizar.
Não substitua o banco enquanto serviços estiverem escrevendo nele.

Atualizações exigem janela: pare `shinyd.target`, faça backup, instale pacotes,
repita a configuração para aplicar migrações e inicie o target. Reiniciar
gateway/entrada HTTPS interrompe WebSockets. A reinstalação não reinicia
automaticamente a pilha. Recuperação automática de uma atualização malsucedida
continua pendente; preserve pacotes anteriores e backup consistente.
Remoção e purge do pacote preservam dados, chaves e configurações; exclusão
permanente deve ser uma ação deliberada do operador. Contas do sistema são mantidas.

O target habilitado reúne os serviços no boot. Recuperação após reboot real
precisa de ensaio próprio; um restart de serviços não comprova esse requisito.
Uma tarefa systemd remove periodicamente sessões e credenciais expiradas.
O target usa `Wants` para iniciar os componentes e estes usam `PartOf` para
acompanhar sua parada explícita. Assim, reiniciar somente painel ou worker
não deve reiniciar o gateway/HTTPS. Essa distinção segue as
[dependências do systemd](https://www.freedesktop.org/software/systemd/man/latest/systemd.unit.html#Wants=).
Um target ativo não é uma verificação de saúde: consulte cada serviço e o painel.

## Homologação descartável

O roteiro recusa instalação com configuração, certificados ou dados existentes,
inclusive arquivos e links de uma preparação parcial. Instala o
pacote, configura HTTPS em `127.0.0.1:18443`, com certificado descartável e nomes
`.test` resolvidos somente no Chrome de teste. Confere TLS e hostname usando a
confiança explícita no certificado; o Chrome aceita somente a chave de teste
fixada por SPKI, sem desativar globalmente validação de certificados.

```sh
go build -o dist/shinyd-proxy-probe ./cmd/shinyd-proxy-probe
set -o pipefail
sudo apt-get --no-remove install crun && \
sudo .venv/bin/python scripts/builder_smoke.py --activation \
  --install-runtime-package dist/shinyd-runtime_0.1.0~runtime4_amd64.deb \
  --install-package dist/shinyd-builder_0.1.0~builder2_amd64.deb \
  --install-control-package dist/shinyd-control_0.1.0~control4_amd64.deb \
  | tee var/installed-report.json
```

O ensaio faz login, cadastro, upload, inspeção, preparação e ativação no navegador,
com worker e executor reais. Verifica cookies seguros, proibição de URL/WS privados,
acesso público, reatividade, afinidade, upload/download e reinício do painel/worker
preservando o processo Shiny. Ao terminar para os serviços e remove somente seus
dados e configurações descartáveis. Pacotes e cache de imagens permanecem.
O teste não usa certificado público, emissão ACME, DNS público, reinício da máquina,
medição de capacidade ou QoS sob carga. O roteiro atual inclui
[segredos em execução](VARIAVEIS_SEGREDOS.md), rotação sem ZIP e remoção.
O roteiro atual também verifica [dados persistentes](DADOS_PERSISTENTES.md).
Drenagem e entrada estável por app continuam pendentes.

Na revisão anterior (`control1`/`runtime2`), as 86 verificações do [CI de 20/10/2026](https://github.com/rfsaldanha/shinyd/actions/runs/39478265938)
passaram, incluindo preservação da sessão durante o reinício de painel/worker.
O job também removeu o pacote e confirmou a preservação de um arquivo de estado.
[Relatório integral](evidencias/installed-https-ci.json).

Ainda na revisão anterior, a execução no host do operador passou em **88 verificações**, em 20/10/2026.
As duas verificações adicionais conferem checksum e identidade do pacote local
do executor. O [relatório local integral](evidencias/installed-https-local.json)
registra preservação da sessão Shiny, dos processos de atendimento e ausência
de erro de limpeza. Uma remoção recebeu HTTP 429 e foi repetida com sucesso.
Ambiente, versões instaladas e hashes estão no marco 19 de [STATUS.md](STATUS.md).
Os serviços do ensaio ficaram parados ao terminar; isso não deixa uma instalação
configurada para atender usuários.


O contrato de segredos, com `control2`/`runtime3`, passou nas **99 verificações** do
[CI de segredos](https://github.com/rfsaldanha/shinyd/actions/runs/39486005117),
incluindo injeção, rotação sem ZIP e remoção de variável. O
[relatório integral](evidencias/secrets-runtime-ci.json) registra o resultado.
A primeira tentativa local de 21/10/2026 parou na instalação do executor por
falta de `crun`, antes dos testes dos apps; o relatório está preservado em
[secrets-dependency-local.json](evidencias/secrets-dependency-local.json).
Após instalar a dependência, a repetição passou nas **102 verificações**, sem
erro de limpeza. São as 99 verificações daquele CI mais a verificação prévia de
`crun`, checksum e identidade do pacote local do executor. O
[relatório local integral](evidencias/secrets-runtime-local.json) comprova entrega,
rotação e remoção de segredos, preservação dos demais apps e das sessões durante
reinício de painel/worker. Os serviços do ensaio ficaram inativos ao terminar;
isso não deixa uma instalação configurada para atendimento permanente.
Detalhes no marco 22 de [STATUS.md](STATUS.md); comando para repetir em
[VARIAVEIS_SEGREDOS.md](VARIAVEIS_SEGREDOS.md).

A revisão `control3`/`runtime4` acrescenta dados persistentes e passou nas
**117 verificações integradas** do [CI de dados](https://github.com/rfsaldanha/shinyd/actions/runs/39598445721).
O [relatório integral](evidencias/data-runtime-ci.json) inclui persistência entre
versões/configurações, recusa de segundo escritor, isolamento entre apps e
preservação dos arquivos ao remover containers. A homologação local passou nas
**119 verificações**, em 21/10/2026, sem erro de limpeza. São as 117 verificações
do CI mais checksum e identidade do pacote local do executor. O
[relatório local integral](evidencias/data-runtime-local.json) registra uma
repetição por executor ocupado no teste de recusa de artefato de outro app;
a recusa esperada foi confirmada. Os serviços do ensaio ficaram inativos ao
terminar. Ambiente e pacotes estão no marco 24 de [STATUS.md](STATUS.md).
Use [DADOS_PERSISTENTES.md](DADOS_PERSISTENTES.md) para repetir o roteiro e gerar
`var/data-report.json` sem sobrescrever relatórios de outros marcos.

A revisão `control4` acrescenta [histórico e rollback](HISTORICO_ROLLBACK.md),
com migração `0009`. Evidências desta revisão estão no marco 25 de
[STATUS.md](STATUS.md); os relatórios anteriores correspondem aos seus próprios
pacotes, não validam automaticamente as novas operações.

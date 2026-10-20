# Desenvolvimento local

Requer Python 3.12+, Go 1.24+ e Linux. Este fluxo inicia somente o painel e a
inspeção de ZIP; não disponibiliza apps Shiny para usuários.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
python3 scripts/bootstrap.py
SHINYD_DEV=1 .venv/bin/python manage.py migrate
SHINYD_DEV=1 .venv/bin/python manage.py createsuperuser
SHINYD_DEV=1 .venv/bin/python manage.py runserver 127.0.0.1:8000
```

Em outro terminal:

```sh
SHINYD_DEV=1 .venv/bin/python manage.py worker
```

Abra `http://127.0.0.1:8000`. Não exponha o modo de desenvolvimento à rede. A senha
do administrador é escolhida interativamente, sem credencial padrão. Crie uma
conta com permissão de publicação para exercitar o papel de publicador.

Se `venv` estiver ausente no Ubuntu, instale `python3-venv`. Arquivos locais ficam
em `var/`, ignorada pelo Git. `bootstrap.py` é idempotente e não substitui chaves.
**Não remova var/keys:** perder a chave mestra torna os segredos irrecuperáveis.

## Verificações

```sh
.venv/bin/pytest -q
.venv/bin/ruff check backend tests scripts manage.py
.venv/bin/ruff format --check backend tests scripts manage.py
SHINYD_DEV=1 .venv/bin/python manage.py check
SHINYD_DEV=1 .venv/bin/python manage.py makemigrations --check --dry-run
go test -race ./...
go vet ./...
```

A configuração de testes usa banco isolado e hash rápido somente em testes.
O código operacional usa Argon2id. Não use `settings_test` para servir o painel.

## Fluxo demonstrável

1. Entrar como administrador; criar publicador e usuário de acesso.
2. Cadastrar app restrito e conceder acesso ao usuário.
3. Enviar ZIP contendo app.R, ou ui.R e server.R.
4. Observar a tarefa na fila; executar worker, inclusive `worker --once`.
5. Conferir resultado e manifesto. "Validado" não significa "publicado".
6. Adicionar segredo, conferir que valor não volta na página/API/auditoria.
7. Usar conta sem concessão: app privado, configurações e trabalhos retornam 404.
8. Enviar ZIP inválido: mensagem de falha, sem executar seu conteúdo.

## Arquitetura em andamento

O código de metadados, autorização e inspeção é independente do contrato de
execução. O isolamento rootful inicial passou; as verificações completas
F0-03–06 ainda têm itens pendentes.
`SHINYD_RUNTIME_ENABLED` permanece falso; não há botão que simule execução.
As fases dependentes desse contrato não estão concluídas.

O [gateway com login central](GATEWAY_ACESSO.md) tem um experimento separado
com dois Shiny reais, banco descartável e acesso por socket Unix. Esse roteiro
não transforma uploads do painel em apps executáveis.

O [agente de diagnóstico](AGENTE_EXECUCAO.md) tem pacote `.deb` e roteiro próprio.
A página “Diagnóstico do servidor” é exclusiva de administradores. Um painel
de desenvolvimento normalmente mostrará “Diagnóstico indisponível”: o agente
de sistema autoriza a conta de serviço `shinyd-control`, não o desenvolvedor.
O teste de navegador abaixo exercita a integração com um agente descartável:

```sh
go build -o dist/shinyd-agent ./cmd/shinyd-agent
.venv/bin/python scripts/browser_smoke.py --agent-binary dist/shinyd-agent
```

Não altera usuários nem serviços do host. O próprio roteiro define as opções
de desenvolvimento, verifica a tela em 360 px e encerra os processos ao terminar.

A verificação de deployment preserva HSTS e cookies seguros. Somente W021
(adesão a HSTS preload) é silenciado: cadastrar o domínio em uma lista global
do navegador requer decisão do administrador, não pode ser um efeito da instalação.

O teste opcional de navegador usa estado e contas descartáveis:

```sh
.venv/bin/pip install playwright==1.64.0
.venv/bin/python scripts/browser_smoke.py
```

Requer Google Chrome em `/usr/bin/google-chrome`. O teste apaga seu banco temporário
e encerra seu servidor ao sair. Não usa o banco local de desenvolvimento.

Fonte do Bootstrap 5.3.8 e HTMX 2.0.11: tarballs do registro npm, integridade
SHA-512 verificada. Assets e respectivas licenças acompanham o repositório;
nenhum CDN é necessário.

O [builder offline](BUILDER_OFFLINE.md) tem pacote e demonstração separados para
preparar ZIPs com `renv.lock`. O contrato CLI passou no CI e no host local com
dois ambientes R. A [integração com o painel](BUILDER_PAINEL.md) agora permite
selecionar um ambiente aprovado e preparar uma versão depois da inspeção.
Exige API instalada e identidade `shinyd-control`; sem isso, a tela informa
indisponibilidade. Preparar uma imagem continua sem ativar a publicação.

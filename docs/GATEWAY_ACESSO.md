# Login central e gateway Shiny

Este marco conecta a autenticação Django ao proxy HTTP/WebSocket. O experimento
original registra instâncias de fixtures. A [ativação pelo painel](ATIVACAO_PAINEL.md)
agora registra instâncias verificadas pelo executor; requer habilitação explícita
de `SHINYD_RUNTIME_ENABLED`, ainda falso por padrão.

## Fluxo de acesso

1. O gateway consulta o controle antes de encaminhar cada requisição, incluindo
   downloads, uploads e abertura de WebSocket. Host desconhecido retorna 404;
   app restrito sem credencial válida retorna 403 com um link para o painel.
2. O usuário entra no painel e confirma a abertura do app em um formulário com
   proteção CSRF. O controle verifica conta, permissão, instância e sessão central.
3. Um código aleatório, válido por até 60 segundos e vinculado à instância,
   segue no corpo de um POST para `/_shinyd/connect`. O banco guarda somente
   seu hash. A troca é de uso único, com atualização condicional no banco.
4. O gateway aceita a troca apenas com a origem exata do painel. O código é
   trocado pelo canal interno; o navegador recebe uma credencial distinta para
   aquela instância e é redirecionado para `/`, sem código na URL.
5. O cookie de produção usa `__Host-shinyd_app`, `Secure`, `HttpOnly`,
   `SameSite=Lax`, caminho `/` e nenhum `Domain`. O cookie do painel fica em outro
   host. Credenciais, cookies reservados e cabeçalhos de identidade não chegam ao R.

A página de transferência usa `Referrer-Policy: strict-origin`: transmite apenas
a origem, sem caminho ou parâmetros. O experimento no Chrome confirmou que
`no-referrer` produz `Origin: null` nesse POST e impede sua validação. Referência:
[algoritmo de Origin no padrão Fetch](https://fetch.spec.whatwg.org/#append-a-request-origin-header).

Não há `next` arbitrário nem redirecionamento fornecido pelo app. A retomada após
login abre a raiz; preservação de links profundos é trabalho posterior.

## Revogação e isolamento

Credenciais de apps duram até uma hora, limitadas também à validade da sessão
central. Não há renovação silenciosa neste marco: ao expirar, o usuário precisa
abrir novamente pelo painel. O prazo deverá ser considerado na política de sessões
do MVP, especialmente para análises longas.

Cada autorização consulta conta, sessão Django e concessões atuais. Logout
remove as credenciais associadas à sessão central. Expiração, troca de senha,
desativação e revisão de sessão invalidam seu uso. Grupos de usuários continuam
participando da autorização. As implementações usam as
[sessões do Django](https://docs.djangoproject.com/en/5.2/topics/http/sessions/) e
[validação do hash de autenticação](https://docs.djangoproject.com/en/5.2/topics/auth/default/#session-invalidation-on-password-change).

WebSockets consultam novamente o controle a cada 20 segundos, com timeout de
2 segundos. Negação, falha do autorizador, desativação da instância ou mudança
de destino encerra a conexão. Apps públicos também são reavaliados: mudar a
visibilidade para restrita interrompe conexões anônimas existentes. O teste real
exige encerramento em menos de 25 segundos. Requisições HTTP já iniciadas não
são canceladas retroativamente; novas requisições são verificadas.

O gateway rejeita origens WebSocket diferentes do host do app. Destinos devem
ser HTTP com IP explícito de loopback e porta, sem credenciais, consulta ou caminho
adicional. Registro de instâncias é responsabilidade de código confiável; não há
formulário ou API pública para cadastrar destinos.

O prefixo `/_shinyd/` é reservado. Service workers são bloqueados para impedir
interceptação de transferências futuras de login; aplicativos que dependem de
PWA não estão homologados. O proxy remove `Clear-Site-Data` e a ampliação de escopo
de service workers, descarta cookies reservados do upstream e retira `Domain`
dos demais cookies. Respostas usam `no-store`; otimização de cache ainda é pendente.

## Comunicação interna

O autorizador possui uma aplicação WSGI separada:
`shinyd_web.gateway_wsgi:application`. Seus endpoints `/v1/authorize` e
`/v1/exchange` **não estão nas rotas HTTP do painel**.

Executar esse serviço somente em socket Unix privado, com chave própria
`gateway.key` e sem access log de corpos/cabeçalhos. O gateway não faz fallback
para TCP, não segue redirecionamentos do autorizador e não reutiliza variáveis
de proxy do ambiente. O corpo interno é limitado a 2 KiB; o POST do navegador,
a 1 KiB. Falhas negam acesso, inclusive a novos acessos públicos.

`bootstrap.py` cria a chave adicional sem sobrescrever chaves existentes.
Ela é diferente das chaves Django e de segredos. Arquivo regular 0600 ou 0640;
permissões dos diretórios/socket devem permitir somente os serviços necessários.
O empacotamento com usuários Unix separados e systemd continua pendente.

As tabelas novas são `AppInstance`, `GatewayTicket` e `GatewaySession`.
Tickets e sessões guardam hashes dos tokens; a associação à sessão Django permite
revogação. A auditoria registra emissão/abertura com IDs, sem códigos ou cookies.
Há limite de cinco transferências pendentes por sessão central e 32 credenciais
vigentes por sessão/instância.

Aplicar migrações e remover registros expirados:

```sh
python3 scripts/bootstrap.py
SHINYD_DEV=1 .venv/bin/python manage.py migrate
SHINYD_DEV=1 .venv/bin/python manage.py cleargateway
SHINYD_DEV=1 .venv/bin/python manage.py clearsessions
```

O agendamento dessa manutenção fará parte do pacote instalável. A configuração
de produção exige `SHINYD_PANEL_ORIGIN=https://painel.exemplo.org` e
`SHINYD_APP_DOMAIN=apps.exemplo.org`; painel e apps devem ter hosts distintos,
mesmo no desenvolvimento. HTTP de desenvolvimento aceita somente `localhost`
e subdomínios de `.localhost`.

O binário `shinyd-gateway` escuta em loopback, destinado à futura entrada HTTPS.
Sua chave e a do autorizador devem coincidir. Alterar a chave exige reiniciar
ambos; reiniciar o gateway interrompe WebSockets e requer janela de manutenção.

## Demonstração reproduzível

Requer as imagens por digest do
[experimento de sessões](DECISOES_EXECUTOR.md#como-reproduzir), Podman rootless,
dependências Python, Playwright 1.64.0 e Chrome. O script não baixa imagens nem
usa o banco/chaves/contas locais do operador.

```sh
go build -o dist/shinyd-gateway ./cmd/shinyd-gateway
.venv/bin/python scripts/gateway_auth_probe.py > var/gateway-auth-report.json
```

Cria banco, chaves, contas e serviços temporários, inicia dois Shiny com R
distintos e abre Chrome. Exercita acesso público, bloqueio por URL e WebSocket,
login central, código de uso único, escopo de cookies, afinidade, upload/download,
mudança de visibilidade, revogação, logout e queda abrupta do autorizador.
Remove seus containers e serviços ao terminar; as imagens permanecem em cache.

O [relatório de 20/10/2026](evidencias/gateway-login-central.json) registra nove
cenários aprovados com R 4.4.3/Shiny 1.10.0 e R 4.5.2/Shiny 1.13.0. Os tempos
medidos de encerramento foram 18,573 s ao restringir um app público, 0,976 s
ao revogar a permissão, 19,958 s no logout e 20,004 s na queda do autorizador.
São observações desse ensaio, não uma medição de capacidade sob carga.

O executor e o builder agora têm contratos separados de execução e preparação.
Ainda faltam instalação completa, TLS real, entrada estável por app, drenagem,
política de renovação e medição de capacidade.
Há uma consulta ao controle por requisição HTTP e por revalidação de WebSocket;
não foi medida capacidade de usuários nem homologado uso em produção.

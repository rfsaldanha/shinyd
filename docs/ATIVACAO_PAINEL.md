# Ativação de versões pelo painel

Este marco conecta a versão preparada ao executor e ao registro de rotas do
gateway. Implementa parte de F2-04/F2-07 e o início de F3-03. O fluxo é:

1. O publicador escolhe **Ativar versão** e confirma o impacto sobre as sessões.
2. O controle cria publicação candidata, instância desabilitada e trabalho
   persistente. Congela a publicação anterior, revisão de configuração,
   prioridade, chave do artefato e checksum do código.
3. O worker consulta o executor com o mesmo UUID em todas as tentativas.
   A primeira partida exige um manifesto protegido do builder e imagem local
   selada. O executor confere isolamento e resposta HTTP antes de retornar
   estado `ready` e socket canônico.
4. O worker confere novamente permissão, cancelamento, configuração e imagem.
   Em uma transação, habilita a rota nova, seleciona a publicação e desabilita
   as rotas anteriores. Falha da candidata preserva a publicação anterior.
5. A instância anterior é encerrada imediatamente. Limpeza malsucedida mantém
   o trabalho visível e é retomada. Uma troca já concluída não pode ser cancelada.

A interface informa que usuários conectados precisarão abrir o aplicativo
novamente. Não há drenagem neste marco. Cada instância tem host próprio, sem
trocar seu destino: credenciais e WebSockets antigos nunca passam para outro
processo R. O botão **Abrir aplicativo** usa o fluxo de login central existente.
A entrada estável `<app>.apps.exemplo.org` prevista no plano ainda não está
implementada: neste marco o host de execução muda em cada ativação.

## Falhas e limites

Uma falha ou parada não dispara nova partida automática da candidata. Uma nova
solicitação cria outro UUID. Perder a resposta ou reiniciar o worker retoma a
consulta; leases impedem que um worker antigo publique depois de outro.

Cancelar exige confirmação do executor. O encerramento definitivo persiste uma
marca antes de remover o container, inclusive quando a partida ainda não chegou.
Pedidos atrasados de partida são recusados após essa confirmação. Os registros
de encerramento permanecem no executor e contam no limite de 256 registros;
coleta segura ainda é pendente. Não apagar esses registros manualmente enquanto
houver workers com solicitações antigas.

A fila compartilhada continua limitada a 100 trabalhos, com uma ativação ou
limpeza por app. Espera por recursos e indisponibilidade não criam processos
extras. Após 15 minutos desde o envio, a candidata é abandonada e sua limpeza
precisa ser confirmada. Consultas pendentes são espaçadas em cinco segundos.
Limpeza pode permanecer pendente se o executor estiver indisponível; não é
declarada concluída sem confirmação. A versão nova já ativada continua
selecionada enquanto a limpeza da anterior é retomada.

O grupo de prioridade é congelado na solicitação; editar a configuração durante
a partida impede a troca. Mudanças posteriores valem para a próxima ativação.
Monitoramento contínuo do estado no painel, drenagem, rollback com dados
persistentes e métricas de sessões ainda exigem os próximos marcos.

**Variáveis e segredos:** armazenamento cifrado já existe, mas a injeção no
processo ainda não faz parte deste contrato. Apps com valores configurados têm
ativação bloqueada explicitamente. Nenhum valor é enviado ao executor nem ao
builder. Não se deve apagar configurações de um app real para contornar essa
restrição; a demonstração remove somente o segredo de sua fixture descartável.
Dados gravados em `/tmp` são temporários; volumes persistentes e saída de rede
continuam indisponíveis.

## Configuração e demonstração

Requer `shinyd-runtime 0.1.0~runtime2`, builder, catálogo aprovado, painel,
worker, autorizador e gateway com identidades e sockets privados. No runtime,
a política deve incluir `builder_artifacts: true`; a opção é falsa por padrão.
No controle, configurar:

```sh
SHINYD_RUNTIME_ENABLED=1
SHINYD_PANEL_ORIGIN=https://painel.exemplo.org
SHINYD_APP_DOMAIN=apps.exemplo.org
```

Os hosts gerados são `p<UUID sem hífens>.apps.exemplo.org`. DNS e HTTPS precisam
cobrir esse domínio. Empacotamento completo, configuração assistida e TLS ainda
não estão concluídos. Modo de desenvolvimento exige hosts `.localhost`
separados. A opção habilita a funcionalidade; não instala nem inicia serviços.
Aplique a migração `0006` após backup do banco.

O roteiro usa instalação de teste vazia, contas e banco descartáveis. Instala
os pacotes explicitamente indicados, testa preparação e ativação pelas views
Django e worker, usa o executor real e exercita a instância ativada no Chrome:

```sh
python3 scripts/build_agent_package.py --component runtime
python3 scripts/build_builder_package.py
go build -o dist/shinyd-proxy-probe ./cmd/shinyd-proxy-probe
set -o pipefail
sudo .venv/bin/python scripts/builder_smoke.py --activation \
  --install-runtime-package 'dist/shinyd-runtime_0.1.0~runtime2_amd64.deb' \
  --install-package 'dist/shinyd-builder_0.1.0~builder2_amd64.deb' \
  | tee var/activation-report.json
```

Os cenários incluem autorização de rota pública/restrita, artefato de outro app
recusado, app que falha na inicialização sem substituir o anterior, cancelamento
antes da partida, recusa de partida atrasada e troca bem-sucedida. O navegador
confere reatividade, upload e download pelo transporte Unix do executor.
A tela nova é exercitada por cliente HTTP Django; o ensaio de navegador usa o
proxy de teste. O fluxo visual completo painel/gateway/TLS em um navegador não
é declarado homologado por esse roteiro.

Ao terminar, o roteiro encerra os serviços de teste e restaura políticas e
limites. Apaga somente as marcas de encerramento dos UUIDs da fixture, depois
que seus processos de controle e executor já terminaram. Imagens ficam no cache.
O relatório registra as verificações e não é uma medição de capacidade.

As 49 verificações do roteiro ampliado passaram no
[CI](https://github.com/rfsaldanha/shinyd/actions/runs/39471110057).
O [relatório](evidencias/activation-ci.json) registra o commit, o ambiente
observado e a limpeza sem erros.

A homologação no host do operador passou em **51 verificações**, em 20/10/2026,
com instalação de `runtime2` e `builder2`. As duas verificações adicionais
conferem o checksum e a identidade do pacote local do executor. O
[relatório local integral](evidencias/activation-local.json) registra ausência
de erro de limpeza e de repetições da API. Os serviços de teste e seus sockets
ficaram inativos ao terminar. Ambiente, hashes conferidos e limites estão no
[marco 17 de STATUS.md](STATUS.md#marco-17--ativação-pelo-painel-homologada-localmente).

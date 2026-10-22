# Histórico e retorno a versões — F3-03

Requer `shinyd-control 0.1.0~control4` e a migração `0009`. O executor continua
`runtime4` e o builder, `builder2`. Evidências executadas e limites estão em
[STATUS.md](STATUS.md). Esta etapa acrescenta rollback de código; não restaura
um backup dos dados.

Passaram **273 testes Python** e **128 verificações integradas no CI**, com
todos os cinco jobs aprovados. Evidência:
[rollback-runtime-ci.json](evidencias/rollback-runtime-ci.json). Na homologação
local com containers, passaram **130 verificações**, sem erro de limpeza nem
repetições por executor ocupado. Evidência:
[rollback-runtime-local.json](evidencias/rollback-runtime-local.json). A prévia
do painel e o teste de navegador sem privilégios também foram verificados localmente.

## Operação pelo navegador

1. Na página do app, abra **Histórico de versões e publicações**. Apenas
   administradores, responsáveis e pessoas com permissão de publicação têm acesso.
2. A seção **Versões enviadas** mostra identificação, data, responsável, estado,
   versão do R, hash do ZIP e imagem preparada. Uma versão pode ser ativada várias
   vezes; cada ativação gera uma publicação diferente.
3. A seção **Publicações** mostra estado, responsável, versão, revisão da
   configuração e acesso ao resultado da tarefa. As duas listas têm paginação
   independente de 20 registros, sem esconder definitivamente os mais antigos.
4. Em uma publicação **Encerrada**, selecione **Voltar a esta versão**. Confira
   qual versão será usada e a revisão atual da configuração. Confirme a
   interrupção das sessões e a responsabilidade pela compatibilidade dos dados.
5. Acompanhe a tarefa e abra a nova publicação pela página do app. As rotas
   anteriores continuam revogadas; cookies e conexões antigos não mudam de destino.

A imagem preparada é reutilizada, sem novo ZIP, download de dependências ou
build. Uma imagem ausente ou inválida resulta em falha da tarefa; não há
reconstrução automática silenciosa. Versões apenas validadas precisam passar
pela preparação antes da ativação. O histórico não inclui ações de exclusão
ou políticas automáticas de retenção nesta etapa.

## Configuração e dados

Rollback cria uma publicação com **a configuração desejada atual**, inclusive
mudanças salvas que ainda não foram aplicadas ao processo em execução. Segredos
rotacionados continuam com os valores atuais e variáveis removidas não reaparecem.
A revisão é capturada e cifrada para o novo identificador de publicação; o
snapshot cifrado de uma publicação antiga não é reutilizado.

A confirmação é assinada, expira em 30 minutos e vincula app, versão de destino,
publicação de origem, publicação ativa e revisões de configuração/app. Se esses
dados mudarem antes do envio, é necessário reabrir a confirmação. Repetir o mesmo
envio usa a mesma tarefa; permissões são revalidadas no envio e pelo worker.
CSRF continua obrigatório. Configuração alterada durante a execução impede uma
troca baseada em decisões antigas.

O fluxo de [dados persistentes](DADOS_PERSISTENTES.md) também vale no retorno:
verificar a candidata com pasta temporária vazia, encerrar essa candidata,
desativar a rota e remover o escritor anterior, abrir o volume real e verificar
a nova instância. Nunca se montam os dados reais em dois escritores simultâneos.

Antes de interromper a publicação anterior, uma falha mantém seu atendimento.
Depois da interrupção, uma falha pode deixar o app indisponível; o volume é
mantido. Uma versão antiga pode ser incompatível com o formato dos dados atual.
Rollback **não desfaz escritas nem migrações de dados**, mesmo quando a nova
partida falha. Faça backup consistente antes de mudanças incompatíveis; a
restauração automatizada pertence a F3-07. Apps que exigem dados pré-carregados
continuam sujeitos à limitação do teste inicial com pasta vazia.

## Auditoria e recuperação

`Publication.restored_from` registra a origem do retorno, enquanto `previous`
identifica a publicação substituída. `rollback.queued` registra apenas IDs e
revisão de configuração; `publication.activated` inclui a origem quando se trata
de rollback. Valores de ambiente não aparecem no histórico ou nesses eventos.
Tentativas que falham ou são canceladas permanecem visíveis com seus resultados.

O retorno usa os mesmos identificadores, checkpoints, verificação de imagem,
limpeza confirmada e retomada do worker que a ativação comum. Interrupção imediata
era o comportamento original desta etapa. `control5` acrescenta a opção de
[aguardar conexões](DRENAGEM_SESSOES.md) também no rollback. Endereço estável
por app e estatísticas de sessões internas do R continuam no backlog.

## Validação reproduzível

Os testes Python cobrem revisão atual, rotação/remoção de segredos, novo
identificador e rota, idempotência, autorização, CSRF, confirmação expirada ou
alterada, paginação, falhas antes/depois da interrupção e revogação durante a
operação. O teste de navegador sem privilégios cobre acesso ao histórico e tela
de 360 px:

```sh
.venv/bin/pytest -q tests/test_history.py tests/test_activation.py tests/test_persistent_data.py
.venv/bin/python scripts/browser_smoke.py
```

O roteiro instalado usa HTTPS e R reais: publica versões diferentes, grava um
arquivo, remove um segredo antigo, altera a configuração em outra aba, recusa
uma confirmação desatualizada e executa o retorno. Verifica imagem reutilizada,
ausência de novos uploads/builds, dados e configuração atuais, segredo removido
ausente, rota anterior revogada e outra sessão Shiny ainda reativa. Os resultados
efetivamente executados são registrados separadamente no STATUS.

A execução local de 21/10/2026 confirmou as 128 verificações do CI e acrescentou
checksum e identidade do pacote local do executor. Usou Ubuntu 24.04.5 LTS,
kernel `7.0.0-38-generic`, 16 CPUs lógicas, 66.735.198.208 bytes de RAM, ext4,
Podman 4.9.3 e crun 1.14.1. O retorno preservou o arquivo e a configuração
pendente, não recuperou o segredo removido, revogou a rota substituída e manteve
outra sessão Shiny reativa. Os serviços do ensaio ficaram inativos ao terminar;
a prévia independente em `http://127.0.0.1:8010` continuou respondendo.
Isso é validação funcional, sem medição de capacidade, reboot ou drenagem.

Para repetir em uma instalação de teste vazia, após construir os pacotes:

```sh
set -o pipefail
sudo .venv/bin/python scripts/builder_smoke.py --activation \
  --install-runtime-package dist/shinyd-runtime_0.1.0~runtime4_amd64.deb \
  --install-package dist/shinyd-builder_0.1.0~builder2_amd64.deb \
  --install-control-package dist/shinyd-control_0.1.0~control5_amd64.deb \
  | tee var/rollback-report.json
```

Esse roteiro recusa instalações com configuração ou dados existentes. Não o
execute contra a instalação usada para servir usuários. As fixtures são
descartáveis; serviços e dados do ensaio são limpos ao terminar. A prévia local
do navegador usa outro banco em `var/` e não fornece acesso privilegiado ao runtime.

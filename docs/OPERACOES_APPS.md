# Iniciar, parar e reiniciar — F3-05

O pacote `shinyd-control 0.1.0~control6`, com migração `0011`, acrescenta
operações individuais e em lote. Usa o executor `runtime4`, o builder `builder2`
e o mesmo contrato de autorização e telemetria de `control5`. As verificações
efetivamente executadas e suas evidências estão em [STATUS.md](STATUS.md).

Passaram **310 testes Python**, **155 verificações integradas no CI** após o
ajuste do roteiro e **157 na homologação local**, em 22/10/2026. Ambos os ensaios
aprovaram as 101 verificações da instalação/navegador e as 13 da ativação,
sem erro de limpeza. F3-05 está homologada nesses ambientes. Estão preservados
o [relatório local](evidencias/lifecycle-runtime-local.json), o
[relatório do CI após o ajuste](evidencias/lifecycle-runtime-ci-retry.json) e a
[primeira tentativa local](evidencias/lifecycle-runtime-local-initial.json).

O roteiro repete somente `runtime_busy`, até quatro tentativas com o mesmo ID
e cinco segundos entre elas; exige `data_in_use` para aprovar a proteção do
volume. Registra os resultados em `installed.data_second_writer_outcomes` e
o nome da verificação que falhou em `installed.failed_check`. A repetição local
aprovada recebeu `data_in_use` na primeira tentativa, sem precisar esperar.
O ensaio é descartável: não deixa atendimento permanente configurado nem
substitui o marco combinado para testes manuais com os apps do usuário.

## Uso pela interface

No catálogo, abra **Operar aplicativos**. Na página de um app, o link
**Iniciar, parar ou reiniciar** já seleciona esse app. Escolha a operação,
o modo de parada/reinício e os aplicativos, ou **Todos os aplicativos sob
minha gestão neste momento**. Publicadores operam somente seus apps e os
que receberam permissão de publicação; administradores podem selecionar todos.

**Revisar impacto** apresenta os nomes, estado publicado, revisão de
configuração, uso de dados persistentes e conexões observadas de cada app.
Essa contagem pode mudar até a execução. A confirmação vale por 30 minutos
e pertence à conta que a abriu. **Confirmar operação** registra uma tarefa
por app; a página de resultados acompanha sua evolução e mostra recusas
individualmente. Uma recusa não impede o envio dos demais. O conjunto fica
congelado na revisão: apps criados depois não entram em “todos”.

| Operação | Comportamento |
|---|---|
| Iniciar | Reutiliza a última versão publicada com sucesso, com a configuração atual e uma nova instância. Não prepara imagem nem exige novo ZIP. App já iniciado permanece como está. |
| Parar | Retira o acesso e remove a instância, preservando versão, configuração e dados persistentes. App já parado permanece como está. Visitar um app parado não o inicia. |
| Reiniciar | Reutiliza o código ativo, aplica a configuração atual, verifica a candidata e troca a instância. App parado exige Iniciar. |

Apps ainda sem publicação precisam primeiro preparar e ativar uma versão.
A migração preenche a versão retida a partir da publicação ativa. Para um app
antigo sem publicação ativa, o operador precisa ativar explicitamente uma
versão antes de usar Iniciar; o sistema não escolhe código entre tentativas
antigas que possam ter falhado.

O modo **imediato** encerra conexões na parada ou na troca do reinício. O
modo **aguardar conexões** impede novas conexões interativas, mantém as
existentes e espera também requisições HTTP em curso. Após cinco minutos,
a operação falha e reabre a entrada na publicação anterior, sem forçar seu
encerramento. Iniciar um app parado não precisa esperar. As restrições de
telemetria estão em [DRENAGEM_SESSOES.md](DRENAGEM_SESSOES.md): conexões não
equivalem a pessoas nem sessões internas do R.

O reinício usa a mesma verificação de uma ativação. Com dados persistentes,
há um teste inicial em pasta temporária; a instância anterior é removida antes
da partida com o volume real. Falha nessa partida pode deixar o app indisponível.
As conexões não migram para o novo processo. Arquivos temporários são descartados;
escritas do app nos dados persistentes não são desfeitas. O volume não é apagado
por parar, iniciar ou reiniciar. Reiniciar aplica também alterações pendentes
de variáveis e segredos, sem recuperar valores de uma publicação antiga.

## Consistência, limites e recuperação

- Há até 100 apps por confirmação e até 100 tarefas globais pendentes. O lote
  mostra os apps recusados se a fila atingir o limite. A fila não cria capacidade
  adicional: o executor continua aplicando o controle de admissão existente.
- Cada app admite uma ativação, parada ou limpeza por vez. Isso também impede
  habilitar persistência enquanto uma operação anterior está em andamento.
- A confirmação assina IDs, estado ativo, versão retida e revisões. Mudanças
  antes do envio causam recusa por app. A permissão é conferida no envio e antes
  de interromper o atendimento. Resultados ficam ocultos após perda da permissão.
- Repetir a mesma confirmação retorna o mesmo lote. As tarefas derivam IDs
  estáveis do lote/app; repetir a requisição não reinicia novamente um app.
- Na parada, bloquear a entrada, observar zero conexões e desativar a rota
  usam a mesma transação SQLite `IMMEDIATE`. Não há intervalo em que uma nova
  conexão possa ser admitida depois de observar zero e antes de retirar a rota.
- Antes de chamar o executor, a parada registra seu ponto de confirmação no
  banco: rota retirada, estado parado e remoção pendente. Depois desse ponto,
  cancelamento ou revogação não abandonam a limpeza. O botão de cancelamento
  deixa de aparecer; o operador pode iniciar novamente após concluir a remoção.
- Perda de resposta, reinício do worker ou falha do executor retomam a remoção
  com o mesmo ID. Enquanto a remoção não for confirmada, a tarefa permanece
  visível e bloqueia nova partida desse app. Não há declaração falsa de sucesso
  nem reabertura automática da rota. A retentativa de limpeza ocorre a cada
  cinco segundos; não é reinício automático do app.
- Uma execução antiga do worker não pode gravar resultado após perder sua
  reserva de trabalho. Reservas de conexão só são apagadas após confirmação
  de remoção do processo; tempo sem resposta não prova encerramento.

Lotes não são transações que desfazem alterações já concluídas nos outros
apps. A interface apresenta o resultado de cada tarefa e permite abrir uma
espera para solicitar seu cancelamento. A auditoria registra operação, modo,
IDs e decisões, sem incluir valores de variáveis ou segredos.

## Atualização e demonstração

Siga a janela de manutenção e a preservação de estado/chaves em
[INSTALACAO_INTEGRADA.md](INSTALACAO_INTEGRADA.md). Aplique `0011` antes de
iniciar painel, autorizador e worker. Este marco não altera o protocolo do
executor nem requer novas permissões privilegiadas para o painel.

Verificações sem privilégios:

```sh
.venv/bin/pytest -q tests/test_lifecycle.py tests/test_lifecycle_migration.py tests/test_drain_concurrency.py
.venv/bin/python scripts/browser_smoke.py
```

Os testes cobrem lotes parciais, fila cheia, repetição de confirmação, autorização,
CSRF, alteração de estado, tempo de espera, telemetria incerta, cancelamento,
perda de resposta, retomada, preservação da versão e migração de dados existente.
O teste de concorrência usa duas conexões reais a SQLite com as opções de produção,
nas duas ordens possíveis de admissão e parada.

O roteiro instalado usa duas aplicações Shiny reais. Reinicia uma mantendo a
outra reativa, para com espera e reinicia o worker, inicia por seleção com um
app já ativo, reinicia ambos, para todos, inicia um novamente e confere dados,
configuração, rotas antigas e resultados em tela pequena. Execute somente no
ambiente vazio de homologação, depois de construir os pacotes:

```sh
set -o pipefail
sudo .venv/bin/python scripts/builder_smoke.py --activation \
  --install-runtime-package dist/shinyd-runtime_0.1.0~runtime4_amd64.deb \
  --install-package dist/shinyd-builder_0.1.0~builder2_amd64.deb \
  --install-control-package dist/shinyd-control_0.1.0~control6_amd64.deb \
  | tee var/lifecycle-report.json
```

O ensaio limpa os dados e serviços descartáveis ao terminar. A homologação
local exige `sudo` do operador; não é uma instalação permanente para seus apps.
O marco de teste manual completo combinado continua dependendo de F3-06,
F3-07 e dessa instalação permanente. Este marco não mede capacidade, não adiciona
escalonamento/QoS de Fase 4 e não oferece recuperação automática de crash loops.

# Recuperação de falhas — F3-06

`control7`, migração `0012`, e `runtime5` acrescentam observação de processos e
recuperação opcional por app. O builder continua em `builder2`. O estado da
homologação e os resultados efetivamente obtidos estão em [STATUS.md](STATUS.md).

## Uso pela interface

Na página do app, **Recuperação de falhas** mostra a política, a última
observação, o número de tentativas na sequência, a próxima tentativa e o link
da tarefa. **Configurar recuperação** permite habilitar ou desabilitar, após
confirmação do impacto. Publicadores gerenciam somente seus apps e aqueles
para os quais têm permissão; administradores gerenciam todos. Apps existentes
recebem a política **desabilitada** na atualização, sem iniciar processos.

Uma recuperação executa novamente o código de inicialização do app. Conexões
e sessões da instância encerrada não são restauradas. Arquivos temporários
podem ser perdidos e o app pode escrever novamente nos dados persistentes.
A confirmação informa esses efeitos. A recuperação usa a versão que estava
publicada, sem novo ZIP ou build. Só prossegue quando a configuração desejada
continua igual àquela publicação. Segredos, variáveis, persistência, prioridade
ou revisão do app pendentes exigem aplicação manual; valores antigos não são
restaurados para contornar uma mudança.

## Limites e decisões

- O executor verifica seus containers a cada dois segundos, sujeito ao tempo
  das operações. Distingue processo comprovadamente ausente/encerrado
  (`process_not_running`), encerramento por falta de memória (`process_oom`)
  e falha de inspeção. Ausência de resposta ou registro não prova que seja
  seguro abrir outro processo.
- O worker observa no máximo um app por passagem do laço, depois de processar
  uma tarefa. Cada app aguarda ao menos dez segundos entre observações.
  Uma tarefa demorada pode atrasar essa observação; não há prazo de recuperação
  garantido. A interface informa quando ocorreu a última confirmação.
- Há até **três tentativas automáticas**, com esperas mínimas de **10, 30 e
  60 segundos**. Uma tarefa conta ao entrar na fila, mesmo que a partida falhe.
  A execução respeita a admissão de memória e instâncias do executor. Fila
  cheia aguarda sem gastar uma tentativa; executor ocupado ou perda de resposta
  retomam a mesma tarefa/ID, sem criar processos duplicados.
- A terceira tentativa pode recuperar o app. Uma nova falha antes de dez
  minutos estáveis observados suspende a recuperação. Três partidas malsucedidas
  também a suspendem. Essa suspensão, chamada **circuito aberto**, permanece
  até intervenção, mesmo após reinício do worker. Não expira sozinha.
- Dez minutos estáveis, com observações separadas por no máximo 30 segundos,
  zeram a sequência. Falta de telemetria reinicia a contagem de estabilidade.
  Independentemente disso, no máximo três tarefas automáticas podem ser
  criadas em qualquer janela de dez minutos. Reabilitar a política ou executar
  uma operação manual não apaga esse histórico nem contorna esse limite.
- Após corrigir a causa, o publicador pode salvar a política habilitada para
  rearmar a recuperação, ou **Reiniciar/ativar manualmente**. Se não houver
  publicação ativa, pode usar **Iniciar**. Operações manuais continuam exigindo
  a revisão de impacto e não entram na contagem de tentativas automáticas.
- Falta de memória suspende novas tentativas, inclusive quando acontece no
  teste de partida de uma candidata. Nesse caso, a limpeza termina antes da
  suspensão definitiva. Esta fase não adiciona a política global de pressão
  de memória, escalonamento ou QoS da Fase 4.
- Perda de permissão da conta que habilitou a política suspende a recuperação.
  O worker confere a permissão novamente na tentativa e na troca. Ações de
  observação são auditadas como sistema; habilitação guarda o responsável.

## Concorrência, dados e interrupções

As observações usam uma reserva no SQLite e um identificador de execução.
A decisão final confere novamente a reserva, o app, a publicação, a intenção
de execução e as operações pendentes. Uma observação antiga não pode
ressuscitar um app parado nem sobrescrever a decisão de outro worker. A
reserva expira em 90 segundos para permitir retomada; a fila e os limites
são compartilhados no banco, não na memória do processo.

Operações manuais já pendentes têm precedência. Uma tentativa de recuperação
já na fila aparece como operação em andamento: desabilite a recuperação ou
cancele sua tarefa e aguarde a limpeza para enviar outra operação. Depois da
ativação, a retirada do processo anterior continua até ser confirmada, mesmo
que a política seja desabilitada.

Cada tentativa cria publicação e instância novas, com o protocolo existente
de [ativação e dados persistentes](DADOS_PERSISTENTES.md). O teste inicial usa
dados temporários; o escritor antigo precisa ser removido antes de liberar
o volume para a candidata. Uma falha depois dessa retirada pode deixar o app
indisponível entre tentativas. Dados persistentes não são apagados ou revertidos.
O próximo ensaio retoma a intenção de execução sem reutilizar IDs encerrados.

Parar durante a espera cancela a intenção de execução. Um app parado não
acorda ao receber uma visita. Observação incerta mantém a última rota conhecida
sem abrir outra instância; o gateway continua aplicando autorização. Uma falha
de inspeção pode retirar o relay do executor por segurança; se o processo
continuar vivo, essa situação exige diagnóstico/restauração do relay, sem
reinício automático especulativo. A recuperação não trata travamento lógico
de uma sessão R cujo processo continua vivo.

## Atualização e demonstração

Atualize `runtime5` e `control7` na mesma janela de manutenção seguindo
[INSTALACAO_INTEGRADA.md](INSTALACAO_INTEGRADA.md), preservando dados e chaves.
`control7` depende de `runtime5` para distinguir saída de processo de inspeção
incerta. Aplique `0012` antes de iniciar painel, auth e worker. A migração cria
somente a política desabilitada para apps existentes. O histórico de tarefas
e publicações permanece disponível.

Verificações sem privilégios:

```sh
.venv/bin/pytest -q tests/test_recovery.py tests/test_recovery_migration.py tests/test_recovery_concurrency.py
.venv/bin/python scripts/browser_smoke.py
go test -race ./internal/executor
```

O ensaio instalado publica Shiny real, habilita a política pelo navegador,
encerra o processo três vezes, verifica novas instâncias, dados preservados,
rota anterior fechada e outro app ainda reativo. Reinicia o worker durante
uma espera. A quarta falha deve abrir o circuito; reiniciar o worker não deve
rearmá-lo. A página mostra a suspensão em 360 px. Desabilitar a política e
reiniciar manualmente deve restaurar o atendimento. O ensaio mantém os demais
cenários de publicação, segurança, segredos, dados, rollback e operações.

Execute somente no ambiente vazio e descartável de homologação:

```sh
set -o pipefail
sudo .venv/bin/python scripts/builder_smoke.py --activation \
  --install-runtime-package dist/shinyd-runtime_0.1.0~runtime5_amd64.deb \
  --install-package dist/shinyd-builder_0.1.0~builder2_amd64.deb \
  --install-control-package dist/shinyd-control_0.1.0~control7_amd64.deb \
  | tee var/recovery-report.json
```

O roteiro exige `sudo` para instalar serviços e encerrar somente os containers
de seu fixture. Ao terminar, limpa seus recursos e não deixa atendimento
permanente configurado. Não testa reboot, perda física do servidor, capacidade
ou política global de memória sob carga. O teste manual completo combinado
ainda requer F3-07 e uma instalação permanente de testes.

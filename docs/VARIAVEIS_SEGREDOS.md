# Variáveis e segredos em execução — F3-01

Requer os pacotes experimentais `shinyd-runtime 0.1.0~runtime3` e
`shinyd-control 0.1.0~control2`, com a migração `0007`. O builder permanece
`builder2`: as imagens aprovadas já incluem `jsonlite` e o inicializador R.
O pacote de sistema `crun` é obrigatório, mesmo que o Podman já funcione com
outro executor. `dpkg --install` não instala dependências automaticamente.

## Uso pelo painel

1. Abra um app que você administra e escolha **Adicionar variável ou segredo**.
   Informar um nome existente substitui seu valor. Valores salvos nunca são
   devolvidos ao navegador, inclusive os marcados como variável comum.
2. Salvar incrementa a revisão desejada. O processo atual continua com a revisão
   aplicada, mostrada separadamente. O botão **Aplicar configuração** reutiliza
   a versão preparada: não envia ZIP nem recompila dependências.
3. Confirme a interrupção das sessões. Uma candidata recebe a nova configuração
   e passa pela verificação HTTP antes da troca. Falha preserva a publicação
   anterior; falta de recursos aguarda na fila já limitada do executor.
4. Depois da troca, abra novamente o app pelo painel. O host anterior é revogado
   e o processo anterior é encerrado. Drenagem de sessões será implementada depois.
5. **Remover** exige confirmação e revisão atual. A variável só desaparece do
   ambiente em execução depois de aplicar a configuração.

As cópias cifradas das publicações anteriores permanecem no histórico. Remover uma
variável não apaga essas cópias nem backups já feitos.

Usuários com apenas acesso ao app não podem configurar, remover ou aplicar valores.
As mesmas regras de autorização se aplicam às chamadas diretas aos serviços.
Alterar configuração, permissões ou cancelar durante a ativação invalida a candidata.

Até 64 variáveis por app, 16 KiB por valor e 64 KiB somando nomes e valores em
UTF-8. Valores vazios são aceitos. O contrato suporta aspas, novas linhas e UTF-8;
o campo de senha do formulário é de uma linha. NUL e nomes usados pelo
inicializador, como `PORT`, `PATH`, `RHOME`, `R_*`, `RENV_*` e `SHINYD_*`, são recusados.

## Proteção e contrato de execução

Todos os valores continuam cifrados com AES-GCM e a chave de 32 bytes externa ao
banco. Cada publicação guarda uma cópia também cifrada, vinculada por autenticação
criptográfica ao identificador da publicação, app e revisão. Não há hash público
de valores de baixa entropia. Uma alteração posterior não modifica essa cópia.
Auditoria registra nomes e revisões; jobs e contratos persistidos do executor
guardam apenas identificadores e a revisão, sem valores.

Somente o worker decifra a cópia para a chamada privada `PUT` ao executor.
O socket exige a identidade de controle; consultas e encerramento não transportam
segredos. A API limita tamanho, quantidade e nomes, recusa campos desconhecidos e
JSON ambíguo. Os registros duráveis de cancelamento continuam impedindo partidas
tardias. Perder a resposta não cria outra instância nem reaplica valores diferentes.

O executor cria um `memfd`: arquivo anônimo em memória, sem caminho persistente.
Sela o conteúdo contra escrita e passa exclusivamente esse descritor como FD 3
ao Podman local, usando `--preserve-fds=1` e `/usr/bin/crun` explícito. O inicializador
R fixo interpreta JSON e chama `Sys.setenv` antes de carregar o app; valores nunca
são interpolados como código R ou shell. Não entram em argumentos, `Config.Env`,
imagem, arquivo de ambiente do host ou armazenamento de segredos do Podman.
O descritor só dá acesso à configuração daquela publicação. Continua herdado no
processo; termina com a instância. Não se tenta reiniciar esse container parado:
uma nova publicação fornece novamente a configuração. Recuperação do executor
reconecta apenas a containers que ainda estão vivos, como nos marcos anteriores.

O executor fecha sua cópia do descritor ao finalizar a chamada, inclusive em erro.
Não há garantia de apagar todas as cópias de memória mantidas por Python/Go/R.
Memória e `memfd` podem participar de swap do host; essa entrega não implementa
proteção contra root nem contra leitura física da RAM/swap. Os serviços que tratam
valores e os containers recebem limite `core=0`. Isso não substitui a política
de dumps do host: o Linux pode ignorar esse limite quando encaminha dumps a um
programa, conforme [core(5)](https://man7.org/linux/man-pages/man5/core.5.html).
O kernel, Podman/crun, os serviços de controle e administradores do host fazem
parte da base de confiança.

O código do app e quem pode substituí-lo têm acesso aos próprios segredos.
Um app pode imprimi-los, transformá-los ou devolvê-los em uma página. Não existe
garantia de ocultação contra esse código; não foi implementado um filtro que
prometa sanitizar logs arbitrários. Logs dos containers permanecem restritos
no host, sem exportação pela interface. Mensagens e logs administrativos não
incluem valores. O roteiro de homologação não copia logs de candidatos com segredos
para o relatório de falha.

## Backup, atualização e validação

Backup recuperável precisa incluir banco e `keys/secrets.key` em armazenamento
protegido, além dos demais dados/chaves existentes. Perder a chave impede recuperar
valores e cópias das publicações; não há redefinição silenciosa. Uma chave inválida
ou cifra adulterada impede a nova partida e gera mensagem compreensível.
Consulta, cancelamento e remoção de containers continuam possíveis sem decifrar.
Rotação da chave mestra e recuperação automatizada completa são trabalhos futuros.

A migração adiciona um campo binário sem modificar valores existentes. Publicações
anteriores tinham configuração vazia; permanecem consultáveis e encerráveis pelo
contrato antigo. Atualize o executor antes do controle. Não faça downgrade do
executor enquanto houver registros com o novo campo de revisão.

Testes cobrem autorização, limites, Unicode, integridade criptográfica, revisão,
remoção, retomada, falhas e ausência de valores em respostas/estado/auditoria.
O roteiro integrado verifica também os valores recebidos pelo R por impressões
digitais de dados sintéticos, rotação sem upload, manutenção dos demais processos,
remoção, ausência em metadados/logs e persistência cifrada. Resultados efetivamente
executados são registrados em [STATUS.md](STATUS.md), separados da homologação local.

Para repetir o ensaio no host local após construir os pacotes (instalação
configurada e dados existentes são recusados pelo roteiro):

```sh
set -o pipefail
sudo apt-get --no-remove install crun && \
sudo .venv/bin/python scripts/builder_smoke.py --activation \
  --install-runtime-package dist/shinyd-runtime_0.1.0~runtime3_amd64.deb \
  --install-package dist/shinyd-builder_0.1.0~builder2_amd64.deb \
  --install-control-package dist/shinyd-control_0.1.0~control2_amd64.deb \
  | tee var/secrets-report.json
```

O roteiro remove somente sua configuração e dados descartáveis; pacotes e cache
permanecem. Não altera o banco de desenvolvimento em `var/`.

Se a instalação anterior parou em `dpkg` por falta de `crun`, o comando de APT
acima também configura o `shinyd-runtime` já desempacotado. A simulação local em
21/10/2026 indicou somente a instalação de `crun` e configuração do executor, sem
remoções ou atualizações de outros pacotes. Confira o resumo exibido pelo APT.
Os roteiros agora verificam o pacote e `/usr/bin/crun` antes de instalar qualquer
`.deb`; uma falha nessa verificação não altera pacotes nem inicia os testes.
A primeira tentativa local está preservada em
[secrets-dependency-local.json](evidencias/secrets-dependency-local.json) e não
comprova a execução dos apps: parou antes dela.

Após instalar `crun`, a repetição local de 21/10/2026 passou nas **102 verificações**,
sem erro de limpeza nem repetições por executor ocupado. O
[relatório integral](evidencias/secrets-runtime-local.json) registra a entrega ao
R, rotação sem novo ZIP, remoção, preservação dos demais apps e ausência dos
valores sintéticos nos metadados/logs verificados e no banco em texto claro.
Isso conclui a homologação local de F3-01, sem comprovar capacidade, reboot ou
QoS sob carga. Ambiente e estado final estão no marco 22 de [STATUS.md](STATUS.md).

Fontes oficiais consultadas em 20/10/2026: o
[Podman 4.9.3](https://docs.podman.io/en/v4.9.3/markdown/podman-run.1.html#preserve-fds-n)
documenta a passagem de descritores no cliente local; a
[documentação atual](https://docs.podman.io/en/latest/markdown/podman-run.1.html#preserve-fds-n)
mantém essa opção. A compatibilidade com user namespaces, AppArmor e R é objeto
da execução real do roteiro, não inferida apenas da documentação.
O comportamento do arquivo anônimo e dos selos está descrito em
[memfd_create(2)](https://man7.org/linux/man-pages/man2/memfd_create.2.html).

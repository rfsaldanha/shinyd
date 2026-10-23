# Backup e restauração básicos — F3-07

O pacote `control10` acrescenta `shinyd-backup create`, `verify`, `restore`
e `resume`. É um backup **com os apps e serviços parados**, operado com sudo.
A página administrativa “Backup e restauração” mostra quantos apps, tarefas
e rotas ainda precisam de atenção e o recibo da última restauração.
O painel não recebe a chave de recuperação nem pode ler arquivos arbitrários
do servidor. Agendamento, download e restauração pelo navegador ficam para
F6-01; essa limitação da primeira entrega é explícita.

A implementação e os testes automatizados estão separados da homologação
instalada, registrada em [STATUS.md](STATUS.md). Não use um resultado de
testes unitários como comprovação de recuperação real em outro servidor.

## O que é preservado

* Banco SQLite consistente: usuários, senhas cifradas por hash, permissões,
  apps, versões, histórico, auditoria e configurações cifradas.
* ZIPs recebidos, artefatos do builder e imagens locais imutáveis, incluindo
  ambientes aprovados e imagens necessárias às versões anteriores.
* Dados persistentes em `/var/lib/shinyd-data/<app>/files`, inclusive pastas
  vazias. Arquivos temporários dos containers são descartáveis.
* Chave mestra dos segredos, chaves de transporte, configuração dos serviços,
  políticas aprovadas e certificado/chave TLS, dentro do arquivo criptografado.
* Registros de instâncias já retiradas, para preservar a proibição de reutilizar
  seus identificadores. Instâncias ainda presentes impedem a cópia.

O painel usa o modo WAL do SQLite, que pode manter alterações confirmadas em
um arquivo auxiliar. A cópia usa a API de backup do SQLite para incluir essas
alterações e consolida somente o banco temporário em um arquivo independente.
As conexões são fechadas antes de empacotar ou instalar os arquivos. O banco
original mantém seu modo WAL; os serviços voltam a ativá-lo no banco restaurado.

Logs, caches, tarefas antigas do builder, diretórios temporários, arquivos
de instalação `.deb`, configurações gerais do Linux, DNS, subuids/subgids e
o armazenamento interno do Podman não são copiados. Guarde os pacotes usados
e o roteiro de preparação do host junto do inventário operacional.
Arquivos adicionais fora dos locais documentados, bancos externos e volumes
montados manualmente precisam de procedimentos próprios.

As imagens são exportadas por ID em um único arquivo com
[`podman save --multi-image-archive --format=docker-archive`](https://docs.podman.io/en/v4.9.3/markdown/podman-save.1.html).
Esse modo evita repetir camadas compartilhadas. A saída é comprimida com gzip
enquanto é recebida, sem manter uma cópia descomprimida adicional. A carga usa
[`podman load`](https://docs.podman.io/en/v4.9.3/markdown/podman-load.1.html), que
aceita o arquivo comprimido. Todos os IDs são conferidos após a carga; não há
download de imagem na restauração. O formato interno novo é `shinyd-cold-backup-v2`.
A leitura do inventário v1 continua disponível; restauração continua exigindo
as versões exatas de pacotes registradas na cópia, inclusive `shinyd-control`.

O arquivo usa [`age`](https://github.com/FiloSottile/age), dependência livre
disponível no Ubuntu 24.04, com uma chave pública X25519 para cifrar e uma
identidade privada para recuperar. O formato TAR interno tem versão,
inventário e SHA-256 de cada arquivo. A decifragem deve terminar com sucesso
antes de interpretar o TAR ou carregar imagens. A chave privada age não faz
parte do backup; a chave mestra dos segredos da aplicação fica protegida
dentro dele. Perder a identidade age significa perder a recuperação.

Restaure apenas cópias suas, de procedência conferida: cifrar para uma chave
pública não identifica quem produziu o arquivo. Nunca use esta ferramenta
para abrir uploads enviados por publicadores.

## Preparar uma cópia

Instale `age` e `control10`. Escolha diretórios privados do operador, fora do
estado do shinyd. Exemplo para uma chave nova:

```sh
sudo apt-get --no-remove install age
sudo install -d -m 0700 /root/shinyd-recovery /root/shinyd-backups
sudo age-keygen -o /root/shinyd-recovery/identity.key
sudo age-keygen -y /root/shinyd-recovery/identity.key
```

O último comando mostra a **chave pública** `age1…`. Use esse valor no lugar
do exemplo abaixo; nunca coloque a identidade privada como argumento ou
publique seu conteúdo. Copie a identidade para um local seguro separado,
fora do servidor, e teste sua recuperação. A chave pública pode permanecer
no servidor para novas cópias; não é necessário manter ali a chave privada.

No painel, avise os usuários, abra “Operar aplicativos” e pare todos os apps.
A parada imediata encerra conexões. Se optar por aguardar sessões, confira
os resultados: uma operação cancelada ainda precisa de atenção. Conclua ou
cancele tarefas pendentes. Depois pare a pilha e os acionadores automáticos:

```sh
sudo systemctl stop shinyd.target shinyd-maintenance.timer \
  shinyd-maintenance.service shinyd-panel.service shinyd-auth.service \
  shinyd-worker.service shinyd-gateway.service shinyd-ingress.service \
  shinyd-runtime.service shinyd-builder.service shinyd-panel.socket \
  shinyd-auth.socket shinyd-gateway.socket shinyd-runtime.socket shinyd-builder.socket
sudo shinyd-backup create /root/shinyd-backups/copia-01.tar.age \
  --recipient 'SUBSTITUA_PELA_CHAVE_PUBLICA_age1'
sudo shinyd-backup verify /root/shinyd-backups/copia-01.tar.age \
  --identity /root/shinyd-recovery/identity.key
```

O resultado JSON informa ID da cópia, quantidades e versões necessárias, sem
valores de segredos. `verify` autentica, extrai em área privada, verifica
checksums, código, referências de imagens e a possibilidade de decifrar os
segredos. Não executa o app nem substitui um ensaio de restauração.
Neste marco também exige a janela de manutenção.

Falhas de pré-requisito não param serviços nem containers automaticamente.
Não basta parar o daemon do executor: containers podem continuar funcionando.
Apps devem ser parados pela interface; instâncias de ensaios ou receitas
operadas separadamente precisam ser retiradas pela API do executor.
Estados pendentes de build precisam ser recuperados pelo builder antes
de repetir a cópia. Não apague registros para contornar essas verificações.

O arquivo final nunca sobrescreve outro arquivo e só aparece após cifragem
completa. Transfira-o para armazenamento externo e guarde seu checksum.
Ao concluir, inicie os serviços e os apps desejados pela interface:

```sh
sudo systemctl start shinyd.target
```

## Restaurar em vazio

Prepare um Ubuntu da mesma versão e arquitetura, com os mesmos pacotes
shinyd, Podman e crun informados no resultado do backup. Prepare subuids,
subgids e pré-requisitos de isolamento conforme o guia do executor. A carga
de imagens não configura o kernel nem homologa um host novo.

Instale os pacotes, mas **não execute `shinyd-configure`, não crie a conta
administrativa e não inicie os serviços**. Banco, configurações e dados
devem estar ausentes; os diretórios vazios criados pelos pacotes são aceitos.
Uma instalação ocupada é recusada, sem opção `--force`.

```sh
sudo shinyd-backup restore /root/shinyd-backups/copia-01.tar.age \
  --identity /root/shinyd-recovery/identity.key
```

A ferramenta autentica e valida toda a cópia, exige compatibilidade, carrega
as imagens e só então instala os arquivos. Não inicia serviços. Mantém IDs
dos apps e a chave mestra necessária aos segredos, mas gera novas chaves
Django/gateway, invalida cookies, tickets, sessões e links antigos de
recuperação de senha. As senhas das contas continuam válidas. Apps ficam
parados e a recuperação automática fica desabilitada. O histórico e a última
versão utilizável continuam disponíveis; não se reaplicam operações pendentes.
A auditoria recebe `server.restored`, sem conteúdo dos segredos.

Os dados são atribuídos ao UID/GID 65534 que o executor traduz para o usuário
isolado do container; os diretórios pais de cada app continuam privados de
root. Permissões são normalizadas para leitura/escrita pelo dono, sem bits
especiais ou acesso de outros usuários. ACLs, atributos estendidos e donos
arbitrários não são preservados. Links simbólicos, hardlinks, dispositivos,
sockets, FIFOs e montagens dentro dos dados são recusados, sem seguir links
ou omitir silenciosamente entradas. Reorganize dados incompatíveis antes
de criar uma cópia.

Confira os nomes DNS, políticas de recursos, certificados e capacidade do
host antes de iniciar. Certificado vencido pode ser recuperado para não
impedir o resgate dos dados; renove-o com `shinyd-configure` e os mesmos
nomes/porta/endereço antes de abrir HTTPS. O configurador continua exigindo
certificado válido. Mudança de domínio ou migração entre versões não faz
parte desta restauração inicial.

Inicie `shinyd.target`, entre com a conta existente, confira o recibo em
“Backup e restauração” e inicie primeiro um app. Confira dados, permissões,
segredos pelo comportamento do app e reatividade no navegador. Só depois
retome os demais e reabilite a recuperação automática desejada.

## Espaço, falhas e interrupções

O padrão aceita até 100 mil entradas e 64 GiB para o TAR, ajustável com
`--max-gib` entre 1 e 1024. O limite também é aplicado ao fluxo descomprimido
das imagens; o gzip não permite contorná-lo. O inventário tem limite de 16 MiB.
Há prazo de 30 minutos para exportar imagens e verificações de tamanho e
espaço durante a escrita. As verificações exigem ainda 1 GiB livre; concorrência
externa pode consumir essa reserva, então isso não é uma quota de disco.

Reserve espaço para os dados, o pacote comprimido de imagens, TAR e cópia
cifrada. A cópia privada é liberada antes de cifrar; na restauração, o TAR
decifrado é liberado antes de carregar imagens. O Podman pode precisar do
tamanho descomprimido das camadas em temporários privados e de espaço adicional
em seu armazenamento. Esse espaço temporário é conferido antes da carga.
Não há razão fixa de compressão nem garantia de pico para todos os apps.
Não há cópia incremental ou deduplicação entre backups distintos.

Temporários em `/var/lib/shinyd-backup/work-*` são privados de root e removidos
no encerramento normal, inclusive em erro. Podem conter segredos em claro:
use proteção do disco conforme sua política e não compartilhe esses diretórios.
Erros exibidos não reproduzem conteúdo de arquivos, argumentos de aplicações
ou saída potencialmente sensível de subprocessos.

Durante uma operação, o marcador `maintenance` nesse diretório impede
início dos serviços/sockets/timer pela configuração systemd instalada. Se
o processo for morto, confirme que não está mais em execução e inspecione
os temporários privados antes de usar:

```sh
sudo shinyd-backup resume
```

Esse comando apenas libera uma manutenção interrompida; não inicia serviços
nem remove temporários. Também **recusa** a liberação se existir
`restore-incomplete`. Esse segundo marcador é gravado de forma durável antes
de instalar arquivos e protege também um boot durante restauração parcial.

Em falha normal de escrita, a ferramenta remove apenas os caminhos que
acabou de criar, mantendo os diretórios preexistentes. Imagens já carregadas
podem permanecer no cache; nenhuma imagem é apagada automaticamente.
Se houver queda de energia, KILL ou erro durante a própria limpeza, preserve
o marcador e o estado parcial. O caminho de recuperação suportado é
**restaurar novamente em uma instalação vazia preparada separadamente**,
guardando o estado incompleto para análise. Não há promessa de transação
atômica entre vários diretórios nem retomada automática de cópia parcial.

## Demonstração instalada

Use somente o namespace vazio de homologação descrito no guia de instalação.
O ensaio repete a publicação real e, ao final, cria e verifica o backup,
recusa links/chave errada/arquivo truncado/destino ocupado, guarda o estado
original, retira uma imagem publicada do cache e restaura em vazio. Depois
confere login, dados, segredos e reatividade Shiny pelo Chrome, além do bloqueio
systemd durante restauração incompleta. Não testa reboot real nem capacidade.

```sh
set -o pipefail
sudo .venv/bin/python scripts/builder_smoke.py --activation --backup \
  --install-package dist/shinyd-builder_0.1.0~builder2_amd64.deb \
  --install-runtime-package dist/shinyd-runtime_0.1.0~runtime5_amd64.deb \
  --install-control-package dist/shinyd-control_0.1.0~control10_amd64.deb \
  | tee var/backup-report.json
```

Em falha, o roteiro pode preservar `/var/lib/shinyd-backup-probe-*`, com
permissão 0700, e informar `backup_fixture_retained`. Esse diretório contém
a cópia e o estado original da fixture; não o publique. A cópia só é removida
automaticamente depois da demonstração bem-sucedida. O relatório público
contém resultados, identificadores e hashes, sem arquivos ou chaves.

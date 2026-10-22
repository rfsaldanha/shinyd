# Dados persistentes por app — F3-02

Requer `shinyd-runtime 0.1.0~runtime4`, `shinyd-control 0.1.0~control3` e a
migração `0008`. O builder continua `builder2`. Passaram **117 verificações
integradas no CI**, com relatório em [data-runtime-ci.json](evidencias/data-runtime-ci.json).
Na homologação local, passaram **119 verificações**, registradas em
[data-runtime-local.json](evidencias/data-runtime-local.json).
[STATUS.md](STATUS.md) registra o ambiente e os limites; este resultado não
constitui aprovação para produção.

## Uso pelo painel

Na página do app, escolha **Habilitar dados persistentes** e confirme o impacto.
A opção vale na próxima ativação; para uma versão já ativa, use **Aplicar
persistência e reiniciar**. Somente quem pode publicar nesse app pode habilitar.
Apps existentes permanecem com persistência desabilitada até essa escolha.
Habilitar não importa arquivos nem apaga dados. Não há ação de desabilitar ou
excluir o volume nesta etapa.

O app precisa escrever no diretório indicado por `SHINYD_DATA_DIR`:

```r
diretorio <- Sys.getenv("SHINYD_DATA_DIR")
stopifnot(nzchar(diretorio))
arquivo <- file.path(diretorio, "resultado.rds")
saveRDS(resultado, arquivo)
```

Esse diretório corresponde a `/data` dentro do container. Código e dependências
continuam somente leitura; `/tmp` é descartável. A plataforma não redireciona
escritas relativas nem adapta automaticamente o código dos apps.

## Atualização com um único processo gravando

1. A candidata inicia com uma pasta temporária vazia de até 64 MiB. O teste HTTP
   verifica a partida sem montar o volume real, mesmo quando há uma versão ativa.
2. O worker registra a aprovação desse teste no banco e remove a candidata de
   teste. O identificador fica encerrado definitivamente para impedir partidas
   atrasadas.
3. O worker revalida permissões, configuração e cancelamento; registra a troca e
   desativa a rota anterior. A publicação anterior é removida pelo executor.
4. Só após a confirmação da remoção, uma nova instância recebe o volume real.
   A rota nova é liberada depois das verificações de isolamento e funcionamento.

As sessões existentes são interrompidas. Antecipou-se a remoção confirmada da
instância anterior, parte do ciclo de vida, porque é necessária à segurança dos
dados. Drenagem de sessões continua pendente. Apps sem persistência conservam
o fluxo anterior de verificação e troca.

Falha ou cancelamento antes do passo 3 preserva a publicação anterior e seus
dados. Depois dele, a aplicação pode ficar indisponível. O volume permanece,
mas o código executado com dados reais pode tê-los modificado, mesmo sem passar
na verificação HTTP. Não há transação ou reversão automática de dados do app.
Corrija a causa e solicite outra ativação. Mantenha backup antes de mudanças
incompatíveis de formato. O teste em pasta vazia não comprova compatibilidade
com dados existentes; apps que exigem dados pré-carregados não passam nele.

Os marcos intermediários são duráveis: perda de resposta ou reinício do worker
retoma os mesmos identificadores. O executor recusa outro escritor do mesmo app
até confirmar a remoção do anterior; estado parado, falha ou apenas intenção de
remoção não liberam o volume. Apps diferentes têm reservas independentes.
Várias sessões dentro do mesmo processo ainda podem disputar arquivos: o app
continua responsável por escrita atômica e travas quando necessárias.

## Armazenamento e isolamento

No host: `/var/lib/shinyd-data/<UUID-do-app>/files`. A raiz e o diretório de cada
app pertencem a root, modo 0700. Apenas `files` é montado; o painel, o builder e
os demais apps não recebem esse caminho. A API recebe somente o modo tipado
`persistent` ou `scratch`, e não aceita nomes de volume, caminhos ou opções de
montagem. O artefato continua vinculado ao UUID do app pelo builder.

O volume usa uma montagem com mapeamento de identidades (`idmap`): arquivos em
disco pertencem a UID/GID 65534; dentro de cada container aparecem como o usuário
65534, mapeado para uma identidade subordinada no host. Assim uma nova instância
pode ler o volume sem trocar recursivamente os donos dos arquivos. O executor
não percorre conteúdo, segue links nem executa `chown -R` sobre arquivos do app.
Diretórios controlados por root são conferidos por descritores com `O_NOFOLLOW`.
O conteúdo do volume pode conter links, resolvidos dentro do container.

A montagem usa `nosuid`, `nodev` e `noexec`, junto às proteções já existentes:
usuário sem privilégios, capacidades removidas, AppArmor, raiz somente leitura e
rede isolada. `noexec` não impede um interpretador de ler código; o app já é
código autorizado a acessar os próprios arquivos. Administradores do host fazem
parte da base de confiança.

Depende de Podman rootful/crun, suporte do kernel e do sistema de arquivos a
montagens com mapeamento de identidades. A plataforma de referência continua
Ubuntu 24.04 amd64. Não há fallback silencioso para privilégios maiores ou troca
recursiva de donos. O ensaio real é necessário em cada combinação de host e
sistema de arquivos. Referências consultadas em 21/10/2026:
[Podman 4.9.3, volumes](https://docs.podman.io/en/v4.9.3/markdown/podman-run.1.html#volume-v-source-volume-host-dir-container-dir-options)
e [documentação atual](https://docs.podman.io/en/latest/markdown/podman-run.1.html#volume-v-source-volume-host-dir-container-dir-options).

Antes de cada partida persistente, são exigidos pelo menos 1 GiB livres no
sistema de arquivos dos dados. Isso é controle de admissão, não quota: um app
em execução ainda pode esgotar disco ou inodes. Quotas e proteção contínua de
disco não estão implementadas. Use um sistema de arquivos separado para os dados
quando precisar proteger o espaço do painel; não se promete resistência ao
esgotamento de disco nesta versão experimental.

## Manutenção e validação

Parar/remover containers, trocar código/configuração e desinstalar os pacotes
não apagam os volumes. Não faça downgrade do runtime com registros que contêm
`data_mode`; instale o runtime antes do controle e aplique a migração.
Backup recuperável precisa incluir dados, metadados com os mesmos UUIDs e chaves.
Uma cópia consistente exige interromper a escrita do app. Preserve donos/modos
e links sem segui-los. Backup/restauração automatizados pertencem a F3-07/F6;
não restaure arquivos enquanto uma instância estiver gravando.

O roteiro integrado usa somente fixtures, confere persistência entre versões e
configurações, isolamento entre apps, recusa de outro escritor, falha da candidata
sem tocar dados reais e preservação dos arquivos após remover o container.
Ao terminar, remove explicitamente apenas os volumes descartáveis do ensaio,
depois de confirmar a remoção de seus containers. Não roda sobre instalações
configuradas ou com dados existentes.

A [execução final no CI](https://github.com/rfsaldanha/shinyd/actions/runs/39598445721)
usou kernel `7.0.0-1017-azure`, ext4, Podman 4.9.3 e crun 1.14.1. Os dois apps
usaram faixas de usuários distintas (`327680` e `393216`, com 65536 IDs cada),
com arquivos persistentes em disco pertencentes a UID 65534. Passaram todos os
cinco jobs, sem erro de limpeza; não houve teste de reboot, capacidade ou quotas.

A execução local em 21/10/2026 usou Ubuntu 24.04.5 LTS, kernel
`7.0.0-38-generic`, ext4, Podman 4.9.3 e crun 1.14.1, com 16 CPUs lógicas e
66.735.198.208 bytes de RAM. Confirmou faixas distintas (`655360` e `720896`,
com 65536 IDs cada) e preservação dos dados nas mesmas operações do CI.
As duas verificações adicionais conferem checksum e identidade do pacote local
do executor. Não houve erro de limpeza. O teste de recusa de artefato de outro
app recebeu uma resposta `runtime_busy`, repetiu a tentativa e confirmou a
recusa esperada. Os serviços do ensaio ficaram inativos ao terminar; os pacotes
permanecem instalados, sem configuração para atendimento permanente.

O roteiro atual usa `control5` e inclui [histórico/rollback](HISTORICO_ROLLBACK.md)
e [drenagem](DRENAGEM_SESSOES.md).
As evidências de F3-02 acima se referem a `control3`. Após construir os pacotes,
no diretório do projeto:

```sh
set -o pipefail
sudo apt-get --no-remove install crun && \
sudo .venv/bin/python scripts/builder_smoke.py --activation \
  --install-runtime-package dist/shinyd-runtime_0.1.0~runtime4_amd64.deb \
  --install-package dist/shinyd-builder_0.1.0~builder2_amd64.deb \
  --install-control-package dist/shinyd-control_0.1.0~control5_amd64.deb \
  | tee var/data-report.json
```

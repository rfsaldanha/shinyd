# Builder offline — primeiro contrato

O pacote `shinyd-builder` prepara um ZIP e produz uma imagem OCI local imutável.
É um marco parcial de F2-03: ainda não está ligado ao worker ou aos botões do
painel e não ativa publicações. A operação privilegiada é iniciada pelo operador,
sem conceder sudo ao painel. O catálogo e o estado pertencem a root.

## Entrada e ambiente

A entrada exige ZIP, SHA-256 esperado, UUID do app e um ambiente do catálogo.
O ZIP passa novamente pela extração limitada usada no painel: 500 MiB de entrada,
2 GiB extraídos, 50 mil entradas, sem links, dispositivos, traversal ou colisões.
A pasta selecionada deve conter `app.R` ou `ui.R`/`server.R` e `renv.lock`.
O conteúdo original nunca é executado no host. Arquivos Dockerfile, Containerfile
ou ignore do publicador não governam a montagem.

O catálogo `/etc/shinyd-builder/policy.json`, root e sem escrita de grupo/outros,
contém `schema_version: 1` e `environments`, um mapa de identificadores para:

* `image`: ID local `sha256:...`, nunca uma tag móvel;
* `r_version`: versão exata do R;
* `packages`: nomes e versões instaladas, incluindo shiny, renv e jsonlite.

A imagem base é aprovada pelo administrador, sem credenciais, dados de apps ou
scripts de inicialização externos. R, ferramentas de compilação e bibliotecas
Linux ficam fixados por seu ID. A base e o inventário devem ser obtidos juntos.
Um catálogo incorreto não permite substituir silenciosamente uma versão: o
helper confere novamente o R e os pacotes dentro do container.

Este primeiro contrato admite pacotes CRAN já instalados na base. A biblioteca
privada é preparada com `renv::hydrate`, restaurada com `renv::restore` e conferida
com `renv::snapshot`, incluindo hashes presentes no lock. Não baixa dependências.
Fontes GitHub, caminhos locais, repositórios personalizados, pacotes ausentes ou
versões diferentes são recusados. Sem lock, retorna `lock_required`; detecção
automática e geração de lock pelo painel continuam no backlog.

O cache inicial usa as camadas da base e o artefato completo. Sua chave inclui
app, checksum do ZIP, pasta, ambiente/inventário, lock, versão da receita e hashes
dos helpers. Não existe cache de biblioteca gravável compartilhado entre apps.
Repetir a preparação reaproveita a imagem apenas após conferir sua identidade e
os rótulos controlados. Não promete imagem byte a byte idêntica ao reconstruir.

## Isolamento, recursos e falhas

A montagem usa uma receita da plataforma com COPY, sem RUN ou ADD. A restauração
acontece em container Podman rootful, UID/GID 65534 remapeados, sem rede, volumes
do host, capacidades ou elevação de privilégios. Seccomp, AppArmor imposto,
mapeamento, cgroup e namespace são conferidos no kernel antes da restauração.
Não carrega `.Rprofile`, `renv/activate.R` nem o código do app durante o build.

A camada do container de build é gravável para a biblioteca privada; os arquivos
do app são root:root, somente leitura para seu usuário. `/tmp` é descartável,
limitado a 128 MiB e excluído do commit. O executor mantém sua raiz somente leitura
quando inicia o artefato. Somente os pacotes aprovados da base podem executar seus
hooks de carregamento durante este marco.

Todos os processos de preparação ficam em `shinyd-workloads-maintenance.slice`,
com CPUWeight=50 contra 1000 de interactive. A memória de manutenção é limitada a
1 GiB no agregado, subordinada ao orçamento global já configurado do executor;
o builder nunca aumenta esse orçamento. Há um único build por flock, sem fila
oculta; concorrência retorna `busy`. Requer folga de RAM além da reserva do host,
PSI full avg10 até 1%, orçamento agregado e 6 GiB livres em staging e no storage
do Podman. Reavalia pressão/disco durante comandos longos e cancela sob pressão.
Não é uma garantia de latência nem substitui a política completa da fase 4.

Restauração e montagem têm prazos de 180 segundos por etapa; cada container tem
limite de 128 processos e de 512 MiB por arquivo. O limite por arquivo não é uma
quota de disco: monitoração de espaço e janela entre amostras ainda permitem
picos. Quotas de armazenamento e bibliotecas maiores exigem trabalho posterior.

Etapas e erros usam códigos/mensagens fixos; stdout/stderr de pacotes não vão para
o painel, relatório ou journald. Nenhum segredo configurado no painel é injetado.
Isso não remove credenciais que o próprio publicador tenha incluído no ZIP.

Intenção durável precede criação de containers. Cancelamento tenta remover apenas
recursos com identidade e rótulos correspondentes. Limpeza incompleta mantém o
registro; uma nova invocação reconcilia esse registro antes de trabalhar. Não usa
prune global. Falhas e interrupções não mudam publicação, política do executor ou
dados persistentes. A imagem final e seu manifesto só são registrados após a
restauração. Remoção do pacote preserva catálogos, manifestos e imagens.

## Empacotamento e demonstração

```sh
python3 scripts/build_builder_package.py
```

O pacote depende de `shinyd-runtime`; não habilita serviço nem cria catálogo.
O wrapper `/usr/bin/shinyd-build` inicia um scope systemd de manutenção e executa
Python com `-I`, importando apenas o código instalado. Não use o pacote como
permissão sudo genérica para publicadores.

A demonstração privilegiada usa apenas fixtures do repositório e uma instalação
sem política/instâncias do executor ou builds existentes:

```sh
set -o pipefail
sudo .venv/bin/python scripts/builder_smoke.py \
  --install-package 'dist/shinyd-builder_0.1.0~builder1_amd64.deb' \
  | tee var/builder-report.json
```

O roteiro prepara duas bases R fixadas, instala renv 1.1.5 por tarball oficial com
SHA-256 conferido, gera lockfiles e ZIPs de teste, prepara artefatos e os executa
pelo executor já instalado. Remove somente política/instâncias/manifestações de
teste e restaura os limites agregados anteriores. Mantém pacotes e imagens em
cache; não reinicia o host. Requer Playwright, Chrome e `dist/shinyd-proxy-probe`.
Homologação real e resultados observados são registrados em [STATUS.md](STATUS.md).

## Próximos passos

API tipada de upload/build com autorização por identidade; reconciliação entre
worker e builder; progresso/cancelamento na interface; ativação da candidata e
preservação da publicação anterior. Downloads exigem política de saída que bloqueie
host, metadados e redes privadas, além de catálogo de fontes e checksums. Não abrir
a rede do build como atalho para implementar essa etapa.

Fontes oficiais: [Podman build 4.9.3](https://docs.podman.io/en/v4.9.3/markdown/podman-build.1.html),
[renv restore](https://rstudio.github.io/renv/reference/restore.html),
[renv hydrate](https://rstudio.github.io/renv/reference/hydrate.html).

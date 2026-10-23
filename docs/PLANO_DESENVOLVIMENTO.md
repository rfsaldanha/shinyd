# Plano de desenvolvimento do shinyd

Plano aprovado em 20/10/2026. O progresso e as evidências ficam em
[STATUS.md](STATUS.md); intenções abaixo não são funcionalidades já implementadas.

## Objetivo e escopo

Hospedar e administrar Shiny para R pelo navegador em um único servidor Linux.
Publicadores não precisam conhecer Docker. A operação deve ser open source e
independente de assinatura. Repositório inicial: README mínimo e licença MIT,
preservada. Não há produto anterior a migrar.

Inclui instalação, publicação ZIP, ambientes isolados, autenticação local,
apps públicos/privados, versões, rollback, segredos, dados persistentes,
múltiplas instâncias, QoS, métricas, logs e apresentação institucional opcional.
Exclui clusters, Kubernetes, múltiplos servidores, HA, IDE, máquinas virtuais
como mecanismo de execução e cadastro irrestrito de publicadores.

O protótipo integrado termina na fase 2. O MVP operacional exige fases 1–4,
backup/restauração e verificações essenciais de segurança/documentação da fase 6.
A versão 1.0 exige todas as fases. Não anunciar capacidade de usuários sem medir.

## Arquitetura

* Referência: Ubuntu Server 24.04 LTS amd64, systemd, cgroups v2.
* Administração: Python/Django 5.2 LTS, templates, HTMX e Bootstrap 5 locais.
* Gateway e agente: Go; HTTP/WebSocket sobre `httputil.ReverseProxy`.
* Execução: Podman, backend systemd, um processo R principal por container.
* Entrada HTTPS: Caddy. SQLite/WAL em disco local para metadados.
* Worker e fila persistente no banco, sem Redis/Celery inicialmente.
* Imagens OCI imutáveis com R, bibliotecas de sistema e biblioteca renv.
* Licença MIT; inventário das licenças dos componentes no release.

Separar painel, gateway, worker e agente por serviços. O agente privilegiado
aceita operações tipadas por socket Unix, nunca shell ou parâmetros arbitrários.
Somente o agente acessa o runtime. Portas internas não são publicadas externamente.

Containers: usuário sem root, mapeamento de usuários, identidades distintas por
app, capacidades removidas, no-new-privileges, seccomp/AppArmor, código somente
leitura, temporários limitados e volume por app. Proibir host network, dispositivos,
socket do runtime e montagem arbitrária. Bloquear acesso de rede ao controle,
outros apps e metadados; exceções administradas. Kernel compartilhado não equivale
a isolamento de VM; primeira versão atende publicadores autorizados.

### Reutilização

Comparar ShinyProxy atual, incluindo compartilhamento/pré-inicialização e APIs,
com executor próprio sobre Podman/systemd. Não assumir container por usuário como
única opção. Executor próprio é a recomendação pela integração do QoS hierárquico,
publicação e modelo de dados; F0-03 pode rever essa decisão antes de consolidar
execução. Não manter dois executores. Shiny Server inspira operação e ShinyProxy,
isolamento; shinyapps.io inspira publicação e workers; Posit Cloud, organização
visual. Verificar documentação atual, inclusive mudanças nos produtos.

## Acesso, rotas e interfaces

Painel em `painel.exemplo.org`; entrada estável em `<app>.apps.exemplo.org`;
execução em `<app>-<instancia>.apps.exemplo.org`. DNS wildcard e TLS obrigatório
para uso externo. Certificado fornecido ou ACME DNS, com provedor homologado.
Origem de execução serve app na raiz. Duas abas podem permanecer em versões
distintas. Nome da instância não é credencial.

Login Django, Argon2id, cookies Secure/HttpOnly/__Host- sem Domain compartilhado,
CSRF, validação de Origin e tentativas limitadas. Token temporário, de uso único,
vinculado ao destino troca login central por sessão no host de execução. Não
encaminhar token ao app nem registrá-lo. Verificar autorização em TODA rota,
downloads e abertura WebSocket. Remover cabeçalhos de identidade do cliente.
Revogação fecha WS em até 30 segundos; falha do autorizador nega novos acessos
privados e expira autorizações existentes.

Papéis: administrador do servidor, publicador dos apps atribuídos e usuário de
acesso. Grupos de usuários não são grupos de QoS. Recuperação por SMTP opcional;
sem SMTP, token iniciado pelo administrador. Último administrador recuperável
por comando local privilegiado. Sem cadastro público por padrão.

API `/api/v1`, autorização por objeto, 202 + trabalho para operações assíncronas,
resultados individuais em lotes, revisões para conflitos e identificadores
idempotentes. Agente: criar/inspecionar/drenar/parar/remover/preparar/métricas.
Progresso autenticado com cursor. Gateway só recebe destinos cadastrados.

Modelo: usuários/grupos, apps/permissões, QoS, versões, publicações, revisões de
configuração/segredos, instâncias, alocações, sessões observadas, trabalhos,
auditoria, métricas, apresentação e volumes. Código, configuração e publicação
são entidades separadas.

## Publicação e operação

Receber ZIP → validar → escolher raiz → resolver ambiente → preparar em isolamento
→ criar imagem → testar HTTP e interação Shiny → ativar atomicamente → drenar
anterior → reter para rollback. Falha não altera publicação ativa.

Aceitar app.R ou ui.R + server.R. Ambiguidade de raiz é resolvida na interface.
Defaults: ZIP 500 MiB, extração 2 GiB/50 mil entradas, timeout e contagem real.
Rejeitar absolutos, traversal, links, dispositivos, nomes duplicados ambíguos.
Não expandir ZIPs aninhados. Dockerfile do publicador não governa infraestrutura.
Pacotes também executam código: build isolado sem segredos/dados do runtime.
Bibliotecas de sistema vêm de catálogo aprovado; instalação de pacotes R sem
privilégios. Código/arquivos de credenciais não devem vazar nos logs.

Com renv.lock: respeitar R e pacotes ou informar indisponibilidade, sem upgrade
silencioso. Sem lock: detectar dependências em isolamento, permitir completar
lista pela UI e gerar lock exportável, avisando limites da análise estática.
Registrar R exato, imagem base/digest, pacotes, fontes e bibliotecas de sistema.
Cache chaveado pelo ambiente, sem escrita dos apps. Homologar duas versões R.
Reter imagem permite executar sem fonte remota; reconstrução depende das fontes.

Sessões ficam no processo R; não há migração de memória. Selecionar instância
saudável menos ocupada; manter HTTP/WS/uploads/downloads no mesmo destino.
Reinício imediato informa impacto. Drenagem nega novas sessões e espera as
existentes, sem prazo oculto de encerramento. Operador pode forçar explicitamente.
Parado não acorda por visita; adormecido pode acordar. Operações em lote isolam
falhas. Limite de 3 reinícios/10 min, backoff e circuito aberto após o limite.
Reboot recupera serviços/configuração, não sessões em memória.

Código somente leitura; temporários descartáveis; persistência fora da imagem,
com SHINYD_DATA_DIR. Volume gravável: escritor único por padrão. Paralelismo exige
declaração de suporte a concorrência. Teste de candidata usa volume temporário ou
snapshot. Escrita persistente exige drenagem antes de trocar salvo compatibilidade
declarada. Rollback de código preserva dados e configuração atual. Reverter
configuração é ação separada. Migração do próprio app não tem reversão automática.

Entrega incremental F3-02: antecipa a interrupção imediata confirmada da instância
anterior para garantir um único escritor, com confirmação de impacto no painel.
A candidata usa pasta temporária vazia. F3-04 acrescenta a
[troca com espera das conexões](DRENAGEM_SESSOES.md); falha depois da interrupção pode deixar o app indisponível e não
reverte escritas. O contrato desta etapa está em [DADOS_PERSISTENTES.md](DADOS_PERSISTENTES.md).

Segredos: criptografia autenticada por biblioteca consolidada, chave fora do banco,
revisões, sem leitura posterior na UI, injeção somente ao executar, auditoria sem
conteúdo. Mudança exige reinício/drenagem, não ZIP. Publicador que executa código
pode ler segredos do próprio app. Redação de logs não detecta transformação
arbitrária de segredo; logs são restritos e excluídos das exportações estatísticas.

## QoS e admissão

Hierarquia: shinyd/control e workloads; workloads/interactive e maintenance;
interactive/high(400), normal(200), low(100); cada grupo/app/instâncias/processos.
Control/workloads: 1000:100; interactive/maintenance: 1000:50.
Apps têm peso igual no grupo e instâncias peso igual no app. Sem CPUQuota por app
como mecanismo principal. Mais processos não multiplicam peso agregado.
Sob saturação e paralelismo suficiente, grupos recebem aproximadamente 57/29/14%
da CPU interativa. Grupos ativos usam capacidade ociosa. Não é garantia de latência.
Mudança de grupo aplica por substituição drenada; mostrar desejado e efetivo.

RAM: R física; H=max(1 GiB,15% R); W=R-H; MemoryHigh=90% W, MemoryMax=W no agregado.
Proteção de controle deve ser propagada nos ancestrais. Validar consumo real.
Admissão estima pico observado +50%, ou 512 MiB inicial; reserva contabilizada
impede partidas simultâneas usando a mesma folga. Não é cota obrigatória por app.
Exigir orçamento, MemAvailable e pressão aceitáveis. Swap não é capacidade nova.

Pressão: suspender partidas/builds quando MemAvailable<H; remover ociosas; cancelar
builds se persistir. Crítico inicialmente MemAvailable<H/2 ou PSI full avg10>10%
por 10 s: encerrar baixa→normal→alta, priorizando memória recuperada e poucas
sessões. Registrar perdas; bloquear reposição. Retomar após 30 s estáveis.
Kernel pode agir antes do controlador; não prometer vítima OOM determinística.
Um build simultâneo inicialmente, com orçamento agregado subordinado.

Avaliar escala a cada 5 s: expandir por demanda/ocupação >80%, uma partida/app,
intervalo 30 s, nunca sob saturação/pressão. Reduzir após 5 min sem sessões,
alocações e requisições. Capacidade inicial conservadora: 1 sessão por instância,
ajustável após medir. Mínimo aquecido sujeito a capacidade.

Fila: 200 servidor, 50/app, espera 120 s; rodada 4:2:1 com alternância de apps;
após 60 s, rodada por antiguidade para solicitações elegíveis. Evitar bloqueio
por pedido impossível. Exibir motivo/posição aproximada/cancelar; cheio retorna
Retry-After. Sob sobrecarga permanente não prometer espera finita.

Diagnosticar cpu/memory/pids, PSI, namespaces, AppArmor e árvore efetiva.
io é opcional conforme dispositivo/escalonador. Ausência de requisito obrigatório
bloqueia suporte; não degradar silenciosamente.

## Interface, métricas e manutenção

Comparar injeção HTML, iframe e pacote R. Candidata preferida: shell iframe fora
da origem do painel, sujeito a experimento com apps reais. Até homologação,
modo direto. Não remover CSP silenciosamente. Grid sem cobrir conteúdo; login
antes de embedding; herdar/habilitar/desabilitar por app/grupo. Logo/links
estruturados, sem HTML/JS arbitrário. Testar fluidPage/navbarPage/bslib/
shinydashboard, foco, zoom, menus, modais, uploads, downloads e móvel.

Acesso não é arquivo estático; sessão Shiny não é conexão nem pessoa.
Autenticados por ID; anônimos por estimativa de navegador, sem fingerprint.
Adaptador R homologado para início/fim; cruzar com gateway/runtime. Telemetria
perdida vira desconhecida e impede desligamento por suposta ociosidade.
CPU, RAM, pressão, partidas, falhas, reinícios, duração e fila.
Retenção inicial: 10 s/7 dias; agregados 1 min/90 dias; logs 14 dias;
auditoria 180 dias. Métricas em banco separado, escritas em lotes.
CSV sem credenciais e com neutralização de fórmulas.

.deb verificável e serviços systemd; assistente inicial com token único.
/etc/shinyd para configuração/chaves, /var/lib/shinyd para estado e dados,
/var/cache/shinyd descartável, /run/shinyd para sockets/credenciais temporárias.
Backup consistente de banco, imagens/dados/configuração e chaves; arquivos de
apps exigem parada ou snapshot homologado. Cópia cifrada separada das chaves.
Atualização: verificar, pausar mutações, backup, migrar, verificar, reabrir.
Falha restaura binários e banco antes de novas escritas. Gateway pode interromper
sessões e requer janela. Desinstalação preserva dados; purge é explícito.

## Backlog por fase

P0 bloqueia MVP; P1 bloqueia 1.0; P2 é posterior. Cada aceite exige evidência
reproduzível; nenhum item conclui apenas por uma afirmação.

| ID | Prio | Entregável / aceite | Dependências |
|---|---|---|---|
| F0-01 | P0 | Inventário/plano; MIT preservada e requisitos mapeados | — |
| F0-02 | P0 | Doctor texto/JSON; ausências e erros de permissão distintos | F0-01 |
| F0-03 | P0 | Comparativo executor/ShinyProxy com API e QoS testados | F0-01 |
| F0-04 | P0 | Duas abas/versões, WS e transferências com afinidade | F0-03 |
| F0-05 | P0 | Build/runtime não acessam controle ou dados alheios | F0-02 |
| F0-06 | P0 | Duas versões R/lockfiles reproduzidas com digest | F0-05 |
| F0-07 | P1 | Matriz de barras/layout/dispositivo | F0-04 |
| F1-01 | P0 | Estrutura e CI; checkout limpo compila/verifica | F0-03 |
| F1-02 | P0 | Modelos/migrações/fila sem duplicar trabalho após falha | F1-01 |
| F1-03 | P0 | Pacote/serviços/assistente e recuperação no reboot | F1-02,F0-02 |
| F1-04 | P0 | Login/papéis/recuperação e testes de permissão | F1-02 |
| F1-05 | P0 | Agente tipado e auditoria; recusa chamador/argumento indevido | F1-03 |
| F1-06 | P0 | Dashboard de serviços/disco/controladores | F1-04,F1-05 |
| F2-01 | P0 | Upload seguro; traversal/links/bomba bloqueados | F1-04 |
| F2-02 | P0 | Raiz/ambiente; ambiguidade e R indisponível visíveis | F2-01,F0-06 |
| F2-03 | P0 | Builder/cache; erro não altera app anterior | F2-02,F1-05 |
| F2-04 | P0 | Imagem/readiness com interação Shiny | F2-03 |
| F2-05 | P0 | HTTPS/gateway; URL direta e WS privados protegidos | F2-04,F0-04 |
| F2-06 | P0 | Progresso/cancelamento/logs retomáveis | F2-03 |
| F2-07 | P0 | Reconciliação sem duplicar containers | F2-04 |
| F3-01 | P0 | Segredos sem ZIP/exposição em artefatos | F2-04 |
| F3-02 | P0 | Volumes preservados e isolados | F2-04 |
| F3-03 | P0 | Histórico/ativação/rollback preservam dados/configuração | F3-01,F3-02 |
| F3-04 | P0 | Telemetria/drenagem preserva sessão antiga | F0-04,F2-05 |
| F3-05 | P0 | [Operações/lotes](OPERACOES_APPS.md): confirmação do conjunto/impacto, resultados individuais, parada retomável e dados preservados | F3-04 |
| F3-06 | P0 | [Recuperação de falhas](RECUPERACAO_APPS.md): circuito persistente, até três tentativas, observação conservadora e intervenção pela UI | F2-07,F3-05 |
| F3-07 | P0 | Backup básico restaura app/dados/segredo em vazio; control10 aprovado em 202 verificações no CI, homologação local pendente (marco 42 em STATUS) | F3-01–03 |
| F4-01 | P0 | Distribuição e afinidade de múltiplas instâncias | F3-04 |
| F4-02 | P0 | Peso agregado não aumenta com processos | F1-05,F4-01 |
| F4-03 | P0 | Admissão não reserva a mesma RAM duas vezes | F4-01 |
| F4-04 | P0 | Fila limitada com antiguidade e progresso | F4-03 |
| F4-05 | P0 | Escala sem expansão sob pressão/morte de sessões | F4-03,F3-04 |
| F4-06 | P0 | Crise/OOM registra vítimas e bloqueia reposição | F4-03 |
| F4-07 | P0 | Relatório de concorrência com carga/hardware | F4-02–06 |
| F5-01 | P1 | Retenção/agregações verificadas | F3-04,F4-07 |
| F5-02 | P1 | Gráficos/filtros/CSV conferem com fixture | F5-01 |
| F5-03 | P1 | Contas/grupos/SMTP/revogação fecha WS no prazo | F1-04,F2-05 |
| F5-04 | P1 | Barras homologadas e modo direto | F0-07,F2-05 |
| F5-05 | P1 | Teclado/foco/zoom/360px | F5-02–04 |
| F5-06 | P1 | Erros distinguem pacote/startup/permissão/recursos | F2-06,F4-06 |
| F6-01 | P0/P1 | Backup/chaves; recuperação e falta de chave testadas | F3-07 |
| F6-02 | P1 | Falha de migração restaura versão anterior | F6-01 |
| F6-03 | P0 | Corpus de ataques não acessa controle/outro app | F2-05,F3-01 |
| F6-04 | P0/P1 | Carga/reboot/falhas com perdas e recuperação medidas | F4-07 |
| F6-05 | P0/P1 | Guias permitem instalação/publicação sem contexto | F1-03+ |
| F6-06 | P1 | Release verificável, licenças e componentes | F6-02–05,F5-06 |
| F6-07 | P1 | Uninstall preserva e purge limita-se ao shinyd | F1-03,F6-01 |

Demonstrações: F0 diagnóstico/relatórios; F1 instalar/reboot/login; F2 dois apps
isolados (público/privado) e build com falha; F3 atualizar/drenar/rollback mantendo
arquivo persistente; F4 competição CPU/RAM; F5 roteiro desktop/móvel; F6 Linux
limpo, backup, atualização malsucedida e restauração.

## Validação e riscos

Testar Linux limpo/reboot; dependências distintas; autorização por URL/WS;
reinício isolado; rollback sem sobrescrever dados/segredos; afinidade com PID e
contador; concorrência/QoS e CPU ociosa; RAM/build/startup falhando; barras reais;
restauração e atualização interrompida.

QoS: saturar grupos 180 s após aquecimento, tolerância inicial ±20% relativa;
investigar topologia quando divergir. Capacidade: registrar CPU/núcleos/RAM/swap/
disco/kernel/systemd/runtime/R/pacotes/imagens e processos externos. Perfis leve,
CPU, RAM, I/O e mistura+build. Medir startup frio/quente, fila, p50/p95/p99 de
interação real em navegador, erros/desconexões, pressão e resposta do painel.
Nunca converter requisições HTTP isoladas em usuários Shiny.

Riscos: gateway/afinidade (F0-04), árvore do runtime (inspeção real), kernel
compartilhado (limite de ameaça), OOM brusco (proteção agregada), escrita
concorrente (escritor único), fontes removidas (reter imagens), logs sensíveis
(restrição/redação), SQLite (lotes/banco de métricas), DNS/TLS (assistente),
iframe incompatível (modo direto), host único (backup externo), disco cheio
(retenção/bloqueio preventivo). Experimento negativo bloqueia sua dependência
e requer ADR atualizado; não declarar compatibilidade presumida.

## Extensões

FUT-01 Git/CLI; FUT-02 pacote R; FUT-03 OIDC; FUT-04 templates;
FUT-05 prévias isoladas; FUT-06 agendamentos; FUT-07 Shiny Python;
FUT-08 métricas externas/PostgreSQL se medições justificarem. Todos P2.

## Processo e primeira tarefa

Commits pequenos por tarefa e pushes regulares por marco verificável, após
testes pertinentes. Não versionar credenciais, bancos, caches ou apps privados.
Preservar trabalho existente; não reescrever histórico compartilhado. Falha de
push é informada, mantendo commits locais. Marcos: diagnóstico, fundação,
publicação, acesso privado, dados/versões, escala, QoS, MVP, experiência e 1.0.

Primeira implementação: `shinyd doctor`, somente leitura, texto/JSON e códigos
estáveis; verificar Linux/arquitetura/systemd/cgroups/controladores/PSI/namespaces/
AppArmor/RAM/disco. Ausente, indisponível por permissão e opcional são diferentes.
Demonstrar no host e em fixtures sem controladores, testar, commit e push.

## Referências oficiais

* [Ubuntu](https://ubuntu.com/about/release-cycle)
* [Django](https://docs.djangoproject.com/en/5.2/topics/auth/default/)
* [Go proxy](https://pkg.go.dev/net/http/httputil)
* [Podman](https://docs.podman.io/en/latest/markdown/podman-run.1.html)
* [ShinyProxy](https://shinyproxy.io/documentation/configuration/)
* [Shiny Server](https://docs.posit.co/shiny-server/)
* [shinyapps.io](https://docs.posit.co/shinyapps.io/guide/applications/)
* [Posit Cloud](https://docs.posit.co/cloud/whats_new/)
* [Caddy](https://caddyserver.com/docs/automatic-https)
* [renv](https://rstudio.github.io/renv/articles/docker.html)
* [cgroups v2](https://docs.kernel.org/admin-guide/cgroup-v2.html)
* [systemd](https://www.freedesktop.org/software/systemd/man/latest/systemd.resource-control.html)

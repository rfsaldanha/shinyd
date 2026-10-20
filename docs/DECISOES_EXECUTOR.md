# Executor e proxy: evidências e decisão em andamento

## Reutilização do ShinyProxy

Inspecionados em 20/10/2026:

* ShinyProxy v3.2.4, commit `29438a4218782f72b974b8c93afae85dacb5807b`.
* ContainerProxy v1.2.4, commit `0170bc5c0ef1948cdf8f18da598b8cf039d5523e`,
  dependência declarada pelo ShinyProxy.

A documentação e o código incluem compartilhamento de containers,
pré-inicialização e endpoint administrativo para substituir containers
compartilhados. Não se pressupõe container exclusivo por usuário.

No backend Docker inspecionado, `DockerEngineBackend.startContainer` configura
reserva/limite de memória, período/quota de CPU, rede, portas, usuário e volumes.
Não foi encontrada configuração de `cgroupParent`/peso agregado por grupo no
backend ou no modelo dessa versão. **Inferência:** atender à hierarquia
grupo → app → instância exigiria extensão do backend ou coordenação adicional;
os limites por container existentes não implementam por si só a política proposta.

Fontes:

* [Configuração oficial](https://shinyproxy.io/documentation/configuration/)
* [Dependência declarada](https://github.com/openanalytics/shinyproxy/blob/v3.2.4/pom.xml)
* [Backend Docker inspecionado](https://github.com/openanalytics/containerproxy/blob/v1.2.4/src/main/java/eu/openanalytics/containerproxy/backend/docker/DockerEngineBackend.java)
* [Substituição de containers compartilhados](https://github.com/openanalytics/shinyproxy/blob/v3.2.4/src/main/java/eu/openanalytics/shinyproxy/controllers/DelegateProxyAdminController.java)

A recomendação de agente próprio sobre Podman/systemd permanece. Esta é uma
revisão de documentação/código, não um benchmark operacional de ShinyProxy:
F0-03 ainda não está integralmente aceito.

## Container rootless no host de desenvolvimento

Podman 4.9.3 com systemd/cgroups v2. O teste de isolamento inicial passou:
UID não-root, rede desativada, raiz somente leitura, tmpfs, capacidades removidas,
seccomp, no-new-privileges e limites de 128 MiB/64 processos presentes no kernel.

Foram observados somente `memory` e `pids` delegados ao usuário. AppArmor aparece
como `podman (unconfined)`. Logo, esse modo **não homologa** QoS de CPU nem o
perfil de isolamento proposto.

Relatório sanitizado: [container-rootless.json](evidencias/container-rootless.json).

## Primeiro teste rootful: pré-requisito ausente

O operador executou o teste privilegiado em 20/10/2026. Podman informou cgroups v2,
gerenciador systemd, controlador CPU e AppArmor disponível. A criação do container
falhou: faltavam faixas para `containers` em `/etc/subuid` e `/etc/subgid`.
Isso **não demonstra** AppArmor imposto, mapeamento de UIDs ou peso de CPU
efetivo; essas verificações ficaram sem execução.

[Relatório original](evidencias/container-rootful-prerequisito.json), sem credenciais.
O próximo teste inclui preparação explícita e idempotente dessa reserva,
conforme [o roteiro atualizado](HOMOLOGACAO_CONTAINERS.md). Mantidos user namespaces
e os demais controles de isolamento. Execução de código publicado continua desabilitada.

## Proxy e sessões Shiny reais

O pacote Go `internal/gateway` e o executável experimental `shinyd-proxy-probe`
foram exercitados com dois containers de fixtures confiáveis:

| Instância | R observado | Shiny observado |
|---|---|---|
| a | 4.4.3 | 1.10.0 |
| b | 4.5.2 | 1.13.0 |

Imagens identificadas por digest em `experiments/r-images.json`; versões
observadas pelo próprio app, não presumidas a partir da tag.

Passaram em Chrome real:

* Duas origens/instâncias simultâneas, WebSockets ativos e contadores independentes.
* Requisições posteriores continuam na instância escolhida.
* Upload e download retornam ao processo correto.
* HTTP e abertura de WebSocket sem credencial retornam negação.
* Encerrar a instância a não interrompe a sessão da instância b.

Testes Go adicionais verificam negação por padrão, remoção de cabeçalhos de
identidade/cookies da plataforma, rejeição de alvos fora de loopback, bloqueio
de origem WebSocket indevida e fechamento de uma conexão já atualizada após
revogação da autorização.

Relatório: [shiny-sessoes.json](evidencias/shiny-sessoes.json).

### Como reproduzir

Requer Podman rootless operacional, Go, Playwright e Chrome. Há download de
imagens; as imagens permanecem em cache. Somente fixtures do repositório são
montadas, em leitura, nos containers.

```sh
go build -o dist/shinyd-proxy-probe ./cmd/shinyd-proxy-probe
podman pull docker.io/rocker/shiny@sha256:0c321be169928ed9efdc4f3d39103a9ebec40fb8cbb10ab1bfa2f5154bbb4427
podman pull docker.io/rocker/shiny@sha256:f42fc6fc08cc8b9842bf055fd72ed726e7d738e6a8191a886f8d68ea6cdc27ba
.venv/bin/python scripts/shiny_session_probe.py > var/shiny-session-report.json
```

O proxy experimental aceita somente bind e destinos em loopback e usa uma
credencial temporária de fixture. Não é o login central nem um gateway instalável.
O teste não demonstra restore de renv, drenagem de versões, reconexão de sessão
após queda de rede, coexistência com volumes graváveis ou QoS. F0-04 e F0-06
continuam parcialmente demonstrados; o painel não oferece execução.

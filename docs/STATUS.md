# Estado e evidências

Este arquivo distingue implementação de homologação. O plano completo está em
[PLANO_DESENVOLVIMENTO.md](PLANO_DESENVOLVIMENTO.md).

## Marco 1 — Diagnóstico

* F0-01: plano/backlog registrados; licença MIT preservada.
* F0-02: comando somente leitura com JSON v1, códigos de saída e fixtures.
* Plataforma local observada: Ubuntu 24.04 amd64, systemd, cgroups v2.
* Runtime de containers não homologado: Podman não instalado e sudo exige senha.
* Não há declaração de prontidão para produção.
* Verificação: `go test -race ./...`, `go vet ./...` e build aprovados.
* `shinyd doctor --json`: requisitos observáveis presentes; alerta de Podman
  ausente. Fixtures verificam controlador ausente, permissão negada e disco
  inconclusivo; nenhum recurso foi instalado pelo diagnóstico.

As próximas evidências serão acrescentadas ao concluir cada marco.

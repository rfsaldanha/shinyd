# shinyd

Plataforma open source, em desenvolvimento, para hospedar Shiny para R em um
único servidor Linux. Licença MIT. **Ainda não indicada para produção.**

O [plano e backlog](docs/PLANO_DESENVOLVIMENTO.md) e o
[estado da implementação](docs/STATUS.md) distinguem funcionalidades previstas
de recursos implementados e homologados.

O [guia de desenvolvimento](docs/DESENVOLVIMENTO.md) explica como iniciar o painel
local, criar contas, cadastrar apps e inspecionar um ZIP. O executor ainda não
está habilitado; uma inspeção aprovada não publica o app.

O [isolamento inicial de containers](docs/HOMOLOGACAO_CONTAINERS.md) passou
na verificação rootful. O [gateway com login central](docs/GATEWAY_ACESSO.md)
tem um roteiro separado com apps de teste.

## Diagnóstico do servidor

Em Linux com Go 1.24 ou superior:

```sh
go build -o dist/shinyd ./cmd/shinyd
./dist/shinyd doctor
./dist/shinyd doctor --json --data-dir /var/lib
go test ./...
```

O diagnóstico não modifica o host. Códigos: `0` pré-requisitos obrigatórios
observados; `1` requisito ausente; `2` verificação inconclusiva; `3` uso inválido.
Um resultado positivo não homologa isolamento, delegação de cgroups, rede,
runtime ou capacidade. O diretório de dados informado deve existir.

Commits e pushes são feitos por marcos verificáveis, com os testes pertinentes.

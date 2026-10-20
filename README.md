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
O [executor de containers](docs/EXECUTOR_CONTAINERS.md) acrescenta operações
restritas de ciclo de vida e atendimento por sockets Unix, em pacote separado.
Suas 25 verificações funcionais passaram no CI e no host local.
O [builder offline](docs/BUILDER_OFFLINE.md) prepara ZIPs com `renv.lock` usando
um catálogo de ambientes aprovado. Suas 25 verificações passaram no CI e no
host local, com dois ambientes R distintos. A nova
[integração com o painel](docs/BUILDER_PAINEL.md) acrescenta preparação de versões,
progresso e cancelamento. O roteiro ampliado passou nas 33 verificações no CI e
no host local. A [ativação pelo painel](docs/ATIVACAO_PAINEL.md) acrescenta
partida da candidata, verificação, troca de rota e preservação da publicação
anterior em caso de falha. Exige o pacote `runtime2` e configuração explícita;
o roteiro passou em 49 verificações no CI e 51 no host local, que também
conferiu o pacote do executor antes de instalá-lo. As evidências e limitações
estão em [STATUS.md](docs/STATUS.md).

## Diagnóstico do servidor

O [agente de diagnóstico](docs/AGENTE_EXECUCAO.md) tem pacote `.deb`, socket
privado e integração com a página administrativa “Diagnóstico do servidor”.
Instalação e recuperação do serviço foram verificadas no CI e no host local;
recuperação após reboot continua pendente.
O pacote ainda instala somente esse componente, sem executar uploads.

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

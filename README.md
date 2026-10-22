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
anterior em caso de falha. Exige o pacote `runtime3` e configuração explícita;
o roteiro passou em 49 verificações no CI e 51 no host local, que também
conferiu o pacote do executor antes de instalá-lo. As evidências e limitações
estão em [STATUS.md](docs/STATUS.md).

A [instalação integrada experimental](docs/INSTALACAO_INTEGRADA.md) reúne painel,
worker, autorizador, gateway e HTTPS em serviços systemd. Requer configuração
explícita e certificado. O fluxo instalado passou em 86 verificações no CI e
88 no host local, que também conferiu o pacote do executor antes de instalá-lo.

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

O marco F3-01 acrescenta [variáveis e segredos em execução](docs/VARIAVEIS_SEGREDOS.md),
com aplicação explícita sem novo ZIP e revisão desejada/aplicada no painel.
O fluxo passou em 99 verificações integradas no CI e 102 na homologação local,
após instalar a dependência `crun`. Consulte os marcos 20–22 em
[STATUS.md](docs/STATUS.md).

F3-02 implementa [dados persistentes por app](docs/DADOS_PERSISTENTES.md), com
habilitação pelo painel, teste da candidata em pasta temporária e remoção
confirmada da instância anterior antes de usar os dados reais. Requer `runtime4`
e `control3`. Passaram 117 verificações integradas no CI e 119 na homologação
local. Consulte os marcos 23–24 em [STATUS.md](docs/STATUS.md).

F3-03 acrescenta [histórico e rollback de código](docs/HISTORICO_ROLLBACK.md),
com paginação, confirmação de impacto e auditoria da origem. O retorno reutiliza
a imagem preparada com a configuração atual e preserva o volume de dados.
Requer `control4` e a migração `0009`; não reverte migrações de dados feitas pelo app.
Passaram 128 verificações integradas no CI e 130 na homologação local.
Validação e limites estão nos marcos 25–26 de [STATUS.md](docs/STATUS.md).

F3-04 acrescenta [telemetria de conexões e drenagem](docs/DRENAGEM_SESSOES.md).
Ativação, configuração e rollback permitem aguardar conexões existentes por
até cinco minutos, sem forçar seu encerramento ao fim do prazo. O painel mostra
WebSockets e requisições HTTP em curso; esses números não equivalem a usuários
ou sessões internas do R. Requer `control5`, migração `0010` e atualização
conjunta do gateway/autorizador. O marco 27 de [STATUS.md](docs/STATUS.md)
registra as verificações e a situação da homologação instalada.

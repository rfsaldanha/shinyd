# Homologação de containers

O teste não habilita execução de apps. Sem `--prepare-subids`, não altera
políticas globais do servidor. O roteiro é limitado e revisável em
`scripts/container_smoke.py`.

## Sem privilégios

```sh
python3 scripts/container_smoke.py > var/container-rootless.json
```

Baixa `docker.io/library/alpine:3.22`, registra o digest, cria um container de
teste sem rede, não-root, filesystem somente leitura, 128 MiB e 64 processos.
Verifica os limites efetivos e remove o container ao terminar. A imagem fica
no cache do Podman. Não usa volumes do host nem executa código enviado por usuários.

## Teste privilegiado delimitado

### Pré-requisito: IDs subordinados

O Podman 4.9.3 exige uma reserva chamada `containers` em `/etc/subuid` e
`/etc/subgid` para `--userns=auto` executado como root. Ela permite mapear os
usuários internos para IDs distintos no host, sem mapear root para root.
Não é necessário criar uma conta de login chamada `containers`.
Referência: [documentação oficial de userns](https://docs.podman.io/en/v4.9.3/markdown/podman-run.1.html#userns-mode).

O primeiro teste rootful neste host parou por ausência dessa reserva,
antes de iniciar o container. Instalar os pacotes não criou a reserva.
A evidência foi preservada em
[container-rootful-prerequisito.json](evidencias/container-rootful-prerequisito.json).
Após a preparação, o [reteste](evidencias/container-rootful.json) passou nas 11
verificações. As limitações desse teste continuam descritas ao final deste roteiro.

Para conferir a proposta sem mudar o sistema:

```sh
/usr/bin/python3 scripts/container_subids.py
```

O preparador procura uma faixa livre comum para UID/GID, considerando as
reservas existentes e as contas enumeradas pelo NSS (mecanismo de consulta
de usuários e grupos do Linux). Reserva inicialmente 1.048.576 IDs: 16 blocos
de 65.536, para os experimentos. Isso não mede nem promete capacidade de apps.
Outros containers rootful com `userns=auto` podem consumir o mesmo conjunto.

A preparação explícita:

* Acrescenta somente as entradas `containers`, sem mudar faixas de outros usuários.
* Preserva uma reserva válida existente; não amplia nem move IDs em uso.
* Recusa reservas parciais, conflitantes, arquivos inseguros e provedor `subid`
  externo. Diretórios institucionais que não enumeram todas as contas exigem
  coordenação da faixa com seu administrador antes de usar esse preparador.
* Usa locks de contas, cópias de segurança em diretório privado
  `/etc/shinyd-subids-backup-*` e substituição de cada arquivo com preservação
  de dono/permissões. Não altera o conteúdo de `passwd` ou `group`.
* Restaura a primeira escrita se a segunda falhar. Uma queda abrupta da máquina
  entre as duas substituições exige conferir ambos os arquivos contra o backup;
  a próxima execução recusa a configuração parcial.

### Preparar e testar

Para reservar os IDs, se necessário, e executar o teste no mesmo comando:

```sh
sudo /usr/bin/python3 scripts/container_smoke.py --rootful --prepare-subids > var/container-rootful.json
```

O relatório inclui a reserva e o caminho do backup. Repetir esse comando não
duplica uma reserva válida. A reserva persiste mesmo se o teste posterior falhar;
não deve ser removida enquanto containers ou arquivos dependerem desses IDs.
Antes de restaurar um backup, conferir alterações posteriores de outros
administradores; não sobrescrever o cadastro inteiro sem essa revisão.

Com a reserva já disponível, o teste pode ser repetido sem preparar o sistema:

```sh
sudo /usr/bin/python3 scripts/container_smoke.py --rootful > var/container-rootful.json
```

A senha é solicitada pelo sudo no terminal e não deve ser enviada no chat.
Esse comando adiciona ao teste:

* Uma unidade/slice transitória com prefixo aleatório `shinycheck`, tempo máximo
  de 180 segundos, limite agregado de 192 MiB e 96 tarefas.
* Mapeamento automático de UIDs e verificação do perfil AppArmor em modo enforce.
* Posição real do container sob a slice e peso agregado de CPU 400.

Não instala pacotes, não modifica sudoers, não abre portas, não reinicia serviços
existentes e não acessa dados dos apps. A alteração persistente dos IDs só acontece
com a opção explícita `--prepare-subids`. Limpa somente o container, a unidade e os
overrides transitórios criados pelo próprio teste. A imagem permanece em cache.
O redirecionamento é realizado pelo seu shell, portanto o relatório pertence ao
seu usuário. O relatório não inclui senhas nem variáveis de ambiente.

Se uma verificação falhar, manter o relatório e corrigir a causa antes de repetir.
Não remover controles de segurança para obter um resultado positivo. Se o teste
for interrompido com SIGKILL, verificar unidades/containers com o prefixo registrado
no relatório; as unidades têm prazo, mas o cache de imagem permanece.

Um resultado positivo é somente a etapa inicial de F0-05: ainda são necessários
testes de build, Shiny real, rede do gateway, escape entre apps, pesos sob disputa,
pressão de memória, R/renv e reboot. `production_ready` permanece `false`.

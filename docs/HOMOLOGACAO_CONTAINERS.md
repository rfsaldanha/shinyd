# Homologação de containers

O teste não habilita execução de apps nem altera políticas globais do servidor.
O roteiro é limitado e revisável em `scripts/container_smoke.py`.

## Sem privilégios

```sh
python3 scripts/container_smoke.py > var/container-rootless.json
```

Baixa `docker.io/library/alpine:3.22`, registra o digest, cria um container de
teste sem rede, não-root, filesystem somente leitura, 128 MiB e 64 processos.
Verifica os limites efetivos e remove o container ao terminar. A imagem fica
no cache do Podman. Não usa volumes do host nem executa código enviado por usuários.

## Teste privilegiado delimitado

Após revisar o script:

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
existentes e não acessa dados dos apps. Limpa somente o container, a unidade e os
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

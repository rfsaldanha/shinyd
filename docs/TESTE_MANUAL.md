# Primeiro teste completo pela interface

A homologação funcional da Fase 3 passou no CI e neste servidor. Este roteiro
prepara uma instalação persistente de testes na mesma máquina Linux, usando
os pacotes e imagens já aprovados. Os apps e dados permanecem entre os testes.
O serviço atende apenas `127.0.0.1:18443`; acesso por outra máquina exige uma
configuração de domínio e certificado própria.

## Preparar uma vez

Use o relatório local aprovado em `var/backup-report.json`. O preparador confere
as versões dos pacotes, as imagens por ID, os serviços parados e a ausência de
dados/configuração anteriores. Não baixa imagens ou pacotes. Reaproveita os
ambientes observados no relatório, sem importar usuários ou dados da fixture.
O relatório deve pertencer ao operador e não permitir escrita de grupo/outros.

Antes de aplicar, você pode visualizar a proposta:

```sh
.venv/bin/python scripts/manual_test_setup.py --report var/backup-report.json
```

Para preparar, criar sua conta e abrir o painel:

```sh
cd /mnt/data/drive2/shinyd
chmod go-w var/backup-report.json &&
sudo .venv/bin/python scripts/manual_test_setup.py \
  --report var/backup-report.json --apply > var/manual-test-setup.json &&
sudo shinyd-admin createsuperuser &&
sudo systemctl enable --now shinyd.target &&
.venv/bin/python scripts/open_manual_test.py
```

Escolha usuário e senha quando o comando de criação de conta solicitar.
Não há senha padrão. A senha não é armazenada no relatório nem enviada para
o agente; o Django guarda somente seu hash. Use a conta criada para entrar.
Se um comando falhar, os seguintes não são executados. O resultado da preparação
fica em `var/manual-test-setup.json`, com um código fixo de erro quando recusada.

A janela separada do Chrome abre **https://panel.shinyd.test:18443**. O launcher
confere o certificado e a resposta HTTPS antes de abrir. Resolve os nomes `.test`
somente nessa janela e aceita apenas a chave do certificado local. Não modifica
DNS, `/etc/hosts` ou certificados confiáveis globais. O perfil fica em
`var/manual-browser`, com acesso restrito à sua conta. Abra sempre por esse
comando para usar a resolução e o certificado próprios do teste:

```sh
cd /mnt/data/drive2/shinyd
.venv/bin/python scripts/open_manual_test.py
```

O certificado de teste vale por 90 dias. O launcher recusa certificado expirado;
renovação automática não faz parte deste ambiente inicial.

## Publicar o primeiro app

1. Entre no painel e escolha **Novo aplicativo**. Dê um nome e um endereço ao app.
2. Escolha **Enviar ZIP** e selecione
   `/usr/local/share/shinyd-manual-test/demo-r-a-v1.zip`.
3. Espere a inspeção terminar, escolha **Preparar versão** e selecione o
   ambiente **r-a**, com R **4.4.3**.
4. Depois de preparado, escolha **Ativar versão**, confirme o impacto e aguarde
   a conclusão. Abra o app pelo link da sua página no painel.
5. Use **Somar 1**, envie um arquivo de texto e confira seu conteúdo. O contador
   pertence à sessão atual; um reinício encerra essa sessão.

Para verificar dois ambientes em paralelo, crie outro app com
`demo-r-b-v1.zip` e selecione **r-b**, com R **4.5.2**. Os nomes e versões dos
pacotes aprovados constam de `/usr/local/share/shinyd-manual-test/setup.json`.

## Verificar gestão e persistência

| Ação | Resultado a conferir |
| --- | --- |
| Salvar `APP_MESSAGE` e aplicar a configuração | A mensagem aparece no app após a aplicação; salvar sozinho não reinicia a publicação. |
| Salvar um valor fictício como segredo `APP_TOKEN` | O painel mascara o valor e o app informa apenas se ele está configurado. |
| Habilitar dados persistentes e aplicar a configuração | O botão **Guardar texto** passa a gravar o texto no volume do app. |
| Reiniciar o primeiro app | A sessão reinicia e o texto guardado permanece; o segundo app mantém sua sessão. |
| Enviar `demo-r-a-v2.zip` ao primeiro app e publicar em `r-a` | O título passa a v2 e o texto guardado permanece. |
| Voltar à v1 pelo histórico | O título volta a v1; dados e configuração atual permanecem. |
| Manter uma aba aberta e solicitar troca após as conexões terminarem | A troca aguarda a conexão; fechar a aba permite continuar. O prazo não força sua interrupção. |
| Parar e iniciar uma seleção de apps | Os resultados aparecem individualmente e respeitam a seleção. |
| Criar um usuário sem permissão para um app restrito | O usuário não consegue abrir o app, inclusive pelo endereço direto. |
| Tornar um app público e acessá-lo sem login | O app abre; a administração continua exigindo login. |

Para o teste com outra conta, saia da conta atual nessa janela. A navegação
anônima deve manter as mesmas opções de resolução/certificado do launcher;
uma janela comum do navegador não conhece automaticamente esses nomes `.test`.
O ensaio automatizado já verificou também o bloqueio de WebSockets sem acesso.

## Estado e limites deste ambiente

O preparador configura **4 GiB de memória para o conjunto dos workloads** e
um máximo inicial de **cinco instâncias**. Esses valores limitam o ambiente de
teste e não representam capacidade de usuários. O orçamento não afeta processos
R, PostgreSQL ou outros serviços fora da hierarquia do shinyd. Compartilhamento
de CPU, expansão por múltiplos processos, filas e pressão de memória ainda terão
a validação específica da Fase 4.

Os apps usam os diretórios normais em `/var/lib/shinyd-*`; configurações e chaves
ficam em `/etc/shinyd*` e `/var/lib/shinyd-control`. Os materiais públicos do teste
ficam em `/usr/local/share/shinyd-manual-test`. O recibo de preparação e a cópia
privada do certificado/chave de teste ficam em `/var/lib/shinyd-manual-test`,
acessível apenas a root. O backup do produto inclui o certificado/chave instalados
e os dados dos apps; esses auxiliares do launcher não integram o arquivo de backup.

A primeira preparação exige namespace vazio. Repetir o preparador com o mesmo
relatório e arquivos intactos preserva as chaves; depois de concluído, apenas
confere a configuração, sem reiniciar serviços. Uma falha parcial de configuração
pode ser retomada com o mesmo relatório e serviços parados. Conteúdo divergente,
arquivo incompleto ou origem diferente exige inspeção; o preparador não apaga
esse estado para tentar de novo. Não é uma ferramenta de atualização ou migração.

O catálogo inicial é offline: um app próprio precisa de `renv.lock`, versão exata
do R e pacotes/versões disponíveis no ambiente escolhido. Instalação arbitrária
de dependências e acesso à internet pelos apps ainda não estão habilitados.
As amostras evitam essas dependências externas. Reboot real, atualização com
recuperação automática, desempenho sob carga e prontidão para produção seguem
pendentes; habilitar o target no boot não substitui esses ensaios.

## Pausar ou conferir

Para pausar, pare os apps pela interface antes de parar a pilha:

```sh
sudo systemctl stop shinyd.target shinyd-runtime.service shinyd-runtime.socket \
  shinyd-builder.service shinyd-builder.socket
```

Os dados ficam preservados. Para voltar, inicie `shinyd.target`, entre no painel
e inicie os apps desejados. O roteiro descartável `builder_smoke.py` recusará
essa instalação ocupada; não o use para reinicializar seu teste manual.

Estas verificações são executadas sem sudo e não precisam da sua senha do painel:

```sh
.venv/bin/python scripts/open_manual_test.py --check
.venv/bin/python scripts/open_manual_test.py --check-browser
```

A primeira confere HTTPS e saúde do painel. A segunda também abre o login em um
perfil temporário do Chrome, sem janela, e confere as larguras de 1280 e 360 px.
Nenhuma delas testa login com sua conta, publicação de apps ou capacidade.

# ExternalChannel — Interoperabilidade do MASPY com plataformas multiagentes heterogêneas

Pacote de reprodutibilidade da prova de conceito (PoC) de interoperabilidade
entre três plataformas multiagentes heterogêneas — **MASPY**, **JaCaMo** e
**SPADE** — integradas via HTTP por meio do **ExternalChannel**, uma extensão
do mecanismo de comunicação nativo do MASPY com mensagens baseadas em FIPA-ACL.

O cenário é um fluxo logístico: consulta de CEP, leilão de frete entre
transportadoras e eleição autônoma do veículo de entrega por um protocolo de
anel, com coleta de métricas de latência e qualidade.

---

## Arquitetura

As três plataformas se comunicam por HTTP. Cada uma escuta em uma porta:

| Plataforma | Papel | Porta HTTP |
|------------|-------|------------|
| JaCaMo (jacamo-web / jacamo-rest) | Consulta de CEP e negociação (leilão) | 8080 |
| SPADE | Hub das transportadoras (cálculo de lances) | 5000 |
| MASPY | Frota de veículos (eleição em anel + entrega) | 9000 |

Fluxo de uma execução:

1. O agente `consultor` (JaCaMo) consulta o CEP em uma API pública e informa o
   estado ao agente `negociador`.
2. O `negociador` envia um *Call for Proposal* (CFP) ao hub SPADE.
3. O hub SPADE calcula os lances das transportadoras e devolve as propostas.
4. O `negociador` elege a transportadora de menor custo e envia `accept_proposal`.
5. O hub SPADE aciona a frota MASPY (broadcast).
6. Os veículos MASPY realizam a eleição em anel; o vencedor executa a entrega,
   reporta o resultado ao SPADE e envia rastreamento direto ao JaCaMo.
7. O SPADE encaminha a confirmação final ao JaCaMo.

---

## Dependências

### O que você precisa instalar

| Dependência | Versão usada no experimento | Como instalar |
|-------------|-----------------------------|---------------|
| **Python** | 3.12+ (testado em 3.13.2 e 3.14.5) | <https://www.python.org/downloads/> — no Windows, marque *Add python.exe to PATH* |
| **Java (JDK)** | 17 (LTS) | por exemplo, <https://adoptium.net/> |
| **MASPY** (`maspy-ml`) | 2026.5.13 | `pip install maspy-ml==2026.5.13` |
| **SPADE** | 4.1.2 | `pip install spade==4.1.2` |

Para conferir a instalação, execute em um terminal:

```
python --version
java -version
python -c "import maspy, spade; print('MASPY e SPADE OK')"
```

### O que já vem pronto (não precisa instalar)

- **JaCaMo 0.10-SNAPSHOT com jacamo-rest 0.5:** incluído no `jacamo-web.zip`,
  com o Gradle Wrapper. As bibliotecas do JaCaMo são baixadas automaticamente
  pelo Gradle na primeira execução.
- **Servidor XMPP:** o SPADE 4.1.2 inclui um servidor XMPP local
  (`spade run`), usado por padrão. O experimento original usou uma conta em um
  servidor público, o que continua possível (ver
  [Configuração opcional](#configuração-opcional)).

### Acesso à internet

Necessário para o Gradle baixar as bibliotecas do JaCaMo (primeira execução) e
para a consulta à **API pública de CEP** (BrasilAPI), feita pelo artefato
CArtAgO a cada execução.

Na **primeira execução**, o Gradle baixa a própria distribuição (Gradle 7.6) e
as bibliotecas do JaCaMo — cerca de **340 MB** em disco —, o que pode levar
vários minutos, conforme a conexão. Esses arquivos ficam em cache na pasta
`.gradle` do usuário, e as execuções seguintes iniciam em segundos.

---

## Estrutura do repositório

```
.
├── README.md
├── iniciar_experimento.bat        # Inicia o experimento no Windows
├── maspy/
│   ├── external_channel.py        # ExternalChannel (extensão do Channel do MASPY)
│   ├── veiculo_maspy.py           # Frota de veículos + acionamento das métricas
│   └── metrics_collector.py       # Coleta de métricas (separada do canal)
├── spade/
│   └── transportadoras_spade.py   # Hub das transportadoras
└── jacamo-web.zip                 # Instalação jacamo-web (Java + agentes .asl/.jcm)
```

O `jacamo-web.zip` contém uma instalação do **jacamo-web 0.10-SNAPSHOT** (com
jacamo-rest 0.5) já preparada para o experimento:

- agentes em `examples/logistica/` (`consultor_cep.asl`, `negociador.asl`,
  `gerenciador_frete.jcm`);
- duas classes Java customizadas, que **não** existem em uma instalação padrão
  do JaCaMo: `spade.enviar_msg` (em `src/main/java/spade/`), usada na
  comunicação JaCaMo→SPADE, e `BrasilApiCepClient` (em `src/main/java/`),
  usada na consulta de CEP.

---

## Execução

O agente `consultor` (JaCaMo) dispara o fluxo automaticamente ao iniciar. Por
isso, a ordem importa: o JaCaMo deve ser o **último** a subir, com o servidor
XMPP, a frota MASPY e o hub SPADE já no ar.

Todas as plataformas devem rodar **na mesma máquina** (a classe
`spade.enviar_msg` envia para `http://localhost:5000`, e o hub SPADE escuta
apenas em `localhost`).

### Opção 1 — `iniciar_experimento.bat` (Windows, recomendado)

1. Instale as [dependências](#o-que-você-precisa-instalar).
2. Baixe ou clone este repositório e extraia-o em **qualquer pasta** (o nome da
   pasta pode conter espaços).
3. Dê um duplo clique em **`iniciar_experimento.bat`** (ou execute-o a partir de
   qualquer diretório).

O script executa, em sequência:

| Etapa | O que faz |
|-------|-----------|
| 1/5 | Verifica se Python, Java, MASPY e SPADE estão instalados. Se algo faltar, mostra o comando de instalação e encerra. |
| 2/5 | Na primeira execução, extrai o `jacamo-web.zip` para a pasta `jacamo-web/`, ao lado do script. |
| — | Detecta o IP de rede da máquina, usado pelo jacamo-rest. |
| 3/5 | Abre a janela **XMPP (SPADE)** com o servidor XMPP local e aguarda a porta 5222. |
| 4/5 | Abre a janela **MASPY - Frota** e aguarda a porta 9000; depois abre a janela **SPADE - Hub** e aguarda a porta 5000. |
| 5/5 | Abre a janela **JaCaMo**, que compila o projeto e dispara o fluxo. Na primeira vez, baixa cerca de 340 MB de dependências antes de iniciar (pode levar vários minutos). |

Cada plataforma fica em sua própria janela. O fluxo foi concluído com sucesso
quando a janela **JaCaMo** exibe:

```
[negociador]  SUCESSO! O SPADE confirmou a entrega!
```

Durante a execução, a janela JaCaMo mostra `EXECUTING` na barra do Gradle; isso
é normal enquanto o JaCaMo estiver em execução.

**Para encerrar e obter as métricas:** pressione Ctrl+C na janela
**MASPY - Frota**. O relatório agregado é exibido e salvo em
`maspy/metricas_experimento.csv`. Em seguida, feche as demais janelas
(Ctrl+C em cada uma).

**Para repetir o fluxo** sem reiniciar tudo: na janela JaCaMo, pressione Ctrl+C
(responda `S` à pergunta *Deseja finalizar o arquivo em lotes?*) e execute
novamente:

```
.\gradlew.bat run --args="examples/logistica/gerenciador_frete.jcm"
```

### Opção 2 — Execução manual (Windows, Linux ou macOS)

Use um terminal para cada passo. Os scripts Python podem ser executados de
qualquer diretório; os caminhos abaixo são relativos à raiz do repositório.

1. **Prepare o JaCaMo (apenas na primeira vez):** extraia o `jacamo-web.zip` em
   uma pasta de sua escolha.

   > **Linux/macOS:** o `gradlew` dentro do zip foi salvo com quebras de linha
   > do Windows. Antes do primeiro uso, na pasta extraída, execute
   > `sed -i 's/\r$//' gradlew && chmod +x gradlew` (no macOS, `sed -i ''`).

2. **Servidor XMPP local** (dispensável se você usar outro servidor XMPP):
   ```
   spade run --memory
   ```
3. **Frota MASPY** (porta 9000):
   ```
   python maspy/veiculo_maspy.py
   ```
4. **Hub SPADE** (porta 5000) — aguarde a mensagem
   `Hub de Transportadoras iniciado na porta 5000!`:
   ```
   python spade/transportadoras_spade.py
   ```
   No Windows com o servidor XMPP local, prefira iniciar o hub como faz o
   `.bat` (ver [Solução de problemas](#solução-de-problemas)).
5. **JaCaMo** (porta 8080) — a partir da pasta extraída do `jacamo-web.zip`:
   ```
   .\gradlew.bat run --args="examples/logistica/gerenciador_frete.jcm"
   ```
   No Linux/macOS: `./gradlew run --args="examples/logistica/gerenciador_frete.jcm"`

---

## Trocar o CEP de teste

Nesta PoC, o agente `consultor` usa por padrão o CEP **`01001000`**
(01001-000, São Paulo/SP). Para testar outro CEP:

1. Execute o experimento uma vez (ou extraia o `jacamo-web.zip`), para que a
   pasta `jacamo-web/` exista.
2. Abra em um editor de texto o arquivo
   `jacamo-web/examples/logistica/consultor_cep.asl`.
3. Localize a linha abaixo e substitua `01001000` pelo CEP desejado, mantendo
   as aspas e o ponto final:
   ```
   !consultar_cep("01001000").
   ```
   Por exemplo, para o CEP 20040-002 (Rio de Janeiro/RJ):
   ```
   !consultar_cep("20040002").
   ```
4. Salve o arquivo e execute novamente o JaCaMo (ou o `iniciar_experimento.bat`).
   Não é preciso recompilar.

A janela JaCaMo confirma o CEP usado e o estado retornado pela API:

```
[consultor] Consultando API para o CEP: 20040002
[consultor] Sucesso! Estado: RJ
```

Observações:

- O CEP deve ter **8 dígitos**. Caracteres que não são dígitos (como o hífen)
  são removidos antes da consulta. Um CEP com outra quantidade de dígitos gera
  `[consultor] Erro na API: CEP inválido. Use 8 dígitos.`, e um CEP inexistente
  gera um erro HTTP da API.
- O **estado** retornado define a região usada pelo hub SPADE no cálculo dos
  lances. Por isso, CEPs de outras regiões alteram os custos das
  transportadoras (fatores: Sul 1.0, Sudeste 1.2, Centro-Oeste 1.5,
  Nordeste 1.8, Norte 2.2).
- A alteração vale apenas para a pasta extraída; o `jacamo-web.zip` não é
  modificado. O `.bat` não sobrescreve a pasta `jacamo-web/` já existente. Para
  voltar ao CEP padrão, desfaça a edição ou apague a pasta `jacamo-web/` (ela
  será extraída novamente na próxima execução do `.bat`).

---

## Configuração opcional

Nenhuma edição de código é necessária. Os valores padrão atendem à execução
local; para alterá-los, defina variáveis de ambiente **antes** de executar o
`.bat` ou os scripts.

| Variável | Padrão | Quando usar |
|----------|--------|-------------|
| `IP_JACAMO` | Detectado automaticamente | Se a detecção escolher a interface errada (VPN, várias placas de rede). Use o IP exibido pelo JaCaMo na linha `JaCaMo Rest API is running on http://<IP>:8080/`. |
| `SPADE_JID` | `hub@localhost` | Para usar uma conta em outro servidor XMPP, como no experimento original. Com essa variável definida, o `.bat` não inicia o servidor XMPP local. |
| `SPADE_SENHA` | `senha_hub` | Senha da conta definida em `SPADE_JID`. |

Por que o IP é detectado: o jacamo-rest escuta no **IP de rede da máquina**, e
não em `localhost`. MASPY e SPADE descobrem esse IP da mesma forma que o Java.

Exemplo no Windows (Prompt de Comando), usando uma conta XMPP externa:

```
set SPADE_JID=minha_conta@servidor.xmpp
set SPADE_SENHA=minha_senha
iniciar_experimento.bat
```

No PowerShell, use `$env:SPADE_JID="..."`; no Linux/macOS, `export SPADE_JID=...`.

---

## Solução de problemas

**`DeprecationWarning: 'asyncio.set_event_loop_policy' is deprecated...` (janela SPADE - Hub)**
Aviso esperado no Python 3.14 ou superior, não é um erro. Se, em seguida,
aparecer `[SPADE] Hub de Transportadoras iniciado na porta 5000!`, o hub está
funcionando normalmente.

**Janela SPADE - Hub volta ao prompt sem exibir `Hub de Transportadoras iniciado`**
A conexão com o servidor XMPP local falhou na partida, o que pode ocorrer de
forma intermitente. Feche todas as janelas abertas pelo `.bat` e execute-o
novamente. Se o problema persistir, veja o item *Hub SPADE não inicia* abaixo;
o motivo da falha fica registrado em `spade/spade_xmpp_debug.log`.

**JaCaMo demora a iniciar e baixa muitas dependências**
Comportamento normal na primeira execução (cerca de 340 MB; ver
[Acesso à internet](#acesso-à-internet)). Aguarde até o fluxo começar; as
próximas execuções usam o cache.

**`[consultor] Erro na API: Connect timed out` (janela JaCaMo)**
A consulta à API pública de CEP excedeu o tempo limite de 5 s (comum em redes
instáveis). Na janela JaCaMo, pressione Ctrl+C e execute novamente
`.\gradlew.bat run --args="examples/logistica/gerenciador_frete.jcm"`. MASPY e
SPADE podem continuar abertos.

**Hub SPADE não inicia (`[ERRO] hub SPADE nao respondeu na porta 5000`)**
- Verifique se a rede não bloqueia conexões XMPP; redes institucionais
  costumam bloquear. Uma alternativa é usar outra rede (por exemplo, o
  roteador do celular).
- Com o servidor XMPP local no Windows, há uma condição de corrida no
  pyjabber 0.4.5 durante o STARTTLS que pode travar a conexão. O `.bat` já a
  contorna iniciando o hub com o log de depuração gravado em arquivo
  (`spade/spade_xmpp_debug.log`). Na execução manual, inicie o hub assim:
  ```
  python -c "import logging, runpy; logging.basicConfig(level=logging.DEBUG, filename='spade_xmpp_debug.log'); runpy.run_path('spade/transportadoras_spade.py', run_name='__main__')"
  ```

**Gradle falha com `Unable to establish loopback connection` (Windows)**
Ocorre quando o nome da pasta do usuário contém espaço. O `.bat` já trata isso.
Na execução manual, aponte o diretório temporário do Java para um caminho sem
espaços antes de rodar o Gradle (Prompt de Comando):
`set JAVA_TOOL_OPTIONS=-Djdk.net.unixdomain.tmpdir=C:\Users\Public`

**`[ERRO] MASPY nao encontrado` ou `SPADE nao encontrado`**
Instale as dependências com o comando exibido. Se tiver mais de uma instalação
do Python, confirme que o `pip` usado é o do mesmo `python` do PATH
(`python -m pip install ...`).

---

## Métricas

A frota MASPY coleta quatro grupos de métricas por execução:

- **G1** — latência de injeção (POST → `agent.add`) e baseline do canal nativo
- **G2** — tempo de eleição e número de mensagens no anel
- **G3** — qualidade da eleição (distância do vencedor vs. média) e taxa de acerto
- **G4** — latência end-to-end

As métricas de cada execução aparecem na janela da frota MASPY ao final da
entrega. Ao encerrar a frota (Ctrl+C), as estatísticas agregadas são exibidas e
todas as execuções são exportadas para `maspy/metricas_experimento.csv`.

---

## Observações

- O `notify()` do ExternalChannel é não bloqueante (*fire-and-forget*): falhas de
  rede (servidor de destino fora do ar) são registradas em log e não interrompem
  o ciclo BDI dos agentes.
- O protótipo não usa TLS; é destinado a redes locais confiáveis e ambientes de
  prova de conceito.

Contato com autor:
  João Paulo de Macedo Lepinsk - lepinsk@alunos.utfpr.edu.br

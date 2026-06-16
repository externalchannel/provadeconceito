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

## Pré-requisitos

Versões usadas no experimento:

- **Python 3.12+** (testado em 3.13.2)
- **MASPY** (`maspy-ml`) 2026.5.13 — `pip install maspy-ml==2026.5.13`
- **SPADE** 4.1.2 — `pip install spade==4.1.2`
- **Java** 17 (LTS)
- **JaCaMo** 0.10-SNAPSHOT com **jacamo-rest** 0.5, via projeto **jacamo-web**

Dependências externas em tempo de execução:

- Um **servidor XMPP** acessível para o SPADE. O experimento foi executado com
  uma conta em um servidor público; uma instância local de XMPP também serve.
  Configure as credenciais antes de executar (ver abaixo).
- Uma **API pública de CEP** (consultada pelo artefato CArtAgO do JaCaMo). O
  fluxo depende de acesso à internet para essa consulta.

---

## Estrutura do repositório

```
.
├── README.md
├── maspy/
│   ├── external_channel.py        # ExternalChannel (extensão do Channel do MASPY)
│   ├── veiculo_maspy.py           # Frota de veículos + acionamento das métricas
│   └── metrics_collector.py       # Coleta de métricas (separada do canal)
├── spade/
│   └── transportadoras_spade.py   # Hub das transportadoras
└── jacamo-web.zip                 # Instalação jacamo-web (Java + agentes .asl/.jcm)
```

> Os arquivos do JaCaMo (`consultor_cep.asl`, `negociador.asl`,
> `gerenciador_frete.jcm`) e as duas classes Java já estão dentro do
> `jacamo-web.zip`, em `examples/logistica/` e na árvore de fontes,
> respectivamente.

---

## Integração com o jacamo-web (passo obrigatório)

A comunicação JaCaMo↔SPADE e a consulta de CEP dependem de **duas classes Java
customizadas** (`spade.enviar_msg` e `BrasilApiCepClient`) que **não** existem em
uma instalação padrão do JaCaMo. Para facilitar a reprodução, este repositório
inclui o `jacamo-web.zip`, uma instalação do **jacamo-web 0.10-SNAPSHOT** (com
jacamo-rest 0.5) já contendo as duas classes nos lugares corretos.

1. Extraia o `jacamo-web.zip` em um diretório de sua escolha (requer Java 17).
2. A partir da pasta extraída, compile com o Gradle (o build recria as pastas
   `build/` e `.gradle/` automaticamente).
3. Execute o projeto JaCaMo a partir dessa instalação (ver seção Execução).

> As duas classes já estão posicionadas na árvore de código: `enviar_msg.java`
> em `src/main/java/spade/` (pacote `spade`) e `BrasilApiCepClient.java` no
> diretório de fontes (pacote padrão).

---

## Configuração antes de executar

- **SPADE (XMPP):** em `spade/transportadoras_spade.py`, defina `JID_AGENTE` e
  `SENHA_AGENTE` com uma conta XMPP válida.
- **Endereços de rede:** os arquivos usam `localhost` para SPADE e MASPY e um IP
  de rede local para o JaCaMo (`IP_JACAMO`). Ajuste `IP_JACAMO` em
  `spade/transportadoras_spade.py` e em `maspy/veiculo_maspy.py` para o host onde
  o jacamo-web está rodando.

---

## Execução

A ordem importa: o agente `consultor` (JaCaMo) dispara o fluxo automaticamente
ao iniciar, então o JaCaMo deve ser o **último** a subir, com os serviços que
recebem mensagens (MASPY e SPADE) já no ar.

**Pré-condição:** garanta que o servidor XMPP esteja acessível antes do passo 2
(o agente SPADE se conecta a ele ao iniciar).

1. Inicie a frota MASPY (porta 9000):
   ```
   python maspy/veiculo_maspy.py
   ```
2. Inicie o hub SPADE (porta 5000):
   ```
   python spade/transportadoras_spade.py
   ```
3. Inicie o JaCaMo a partir da pasta extraída do `jacamo-web.zip` (porta 8080):
   ```
   gradlew.bat run --args="examples/logistica/gerenciador_frete.jcm"
   ```
   O `consultor` consulta um CEP de teste e o fluxo completo é disparado.

---

## Métricas

A frota MASPY coleta quatro grupos de métricas por execução:

- **G1** — latência de injeção (POST → `agent.add`) e baseline do canal nativo
- **G2** — tempo de eleição e número de mensagens no anel
- **G3** — qualidade da eleição (distância do vencedor vs. média) e taxa de acerto
- **G4** — latência end-to-end

Ao encerrar (Ctrl+C na frota MASPY), as estatísticas agregadas são exibidas e
todas as execuções são exportadas para `metricas_experimento.csv`.

---

## Observações

- O `notify()` do ExternalChannel é não bloqueante (*fire-and-forget*): falhas de
  rede (servidor de destino fora do ar) são registradas em log e não interrompem
  o ciclo BDI dos agentes.
- O protótipo não usa TLS; é destinado a redes locais confiáveis e ambientes de
  prova de conceito.

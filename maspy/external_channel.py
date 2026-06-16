"""
external_channel.py
===================
Extensão de Conectividade Externa do MASPY


Visão geral:
    Propõe o ExternalChannel como uma extensão do mecanismo de
    Channel do MASPY, adicionando conectividade HTTP sem modificar
    a arquitetura BDI interna do framework. Os agentes continuam
    usando a API nativa; o ExternalChannel traduz mensagens externas
    em eventos BDI e vice-versa.

Recursos:
    - FIPAMessage: envelope de mensagem em conformidade com FIPA-ACL,
      com campos obrigatórios, validação e serialização
    - Performativas suportadas: inform, request, propose,
      accept-proposal, reject-proposal, cfp, agree, failure
    - Fila de prioridade: mensagens processadas por nível de urgência
      (0=urgente, 1=normal, 2=baixa)
    - notify() não bloqueante: despacho fire-and-forget alinhado ao
      comportamento de SPADE e JADE
    - Servidor HTTP assíncrono via aiohttp, substituindo o modelo de
      uma thread por requisição

Início rápido:
    from maspy import *
    from external_channel import ExternalChannel, FIPAMessage

    channel = ExternalChannel.create("my_channel", port=9000)
    channel.route("/delivery", belief="delivery_order",
                  transform=lambda p: (p["carrier"], p["cep"], p["cost"]))

    class DeliveryAgent(Agent):
        @pl(gain, Belief("delivery_order", (Any, Any, Any)))
        def handle_delivery(self, src, data):
            carrier, cep, cost = data
            ch = ExternalChannel.get_channel("my_channel")
            ch.notify(
                "http://localhost:5000/inbox",
                FIPAMessage(
                    performative = "inform",
                    sender       = "agent_1@maspy",
                    receiver     = "hub@spade",
                    content      = f'delivery_done("{carrier}")',
                    priority     = 0
                )
            )

    Admin().connect_to([DeliveryAgent("agent")], [channel])
    Admin().start_system()

Nota de implementação — CommsMultiton:
    O Channel do MASPY usa uma metaclasse Multiton que intercepta
    __call__ e aceita apenas o nome do canal como argumento.
    Para passar a porta sem quebrar esse mecanismo, o ExternalChannel
    a armazena em _pending_ports antes da instanciação; __init__ a lê
    e a remove com pop(). É por isso que ExternalChannel.create() deve
    ser usado em vez de ExternalChannel() diretamente.
"""

from __future__ import annotations

import json
import uuid
import queue
import asyncio
import itertools
import threading
import concurrent.futures
import aiohttp
from aiohttp import web
from datetime import datetime, timezone
from typing import Dict, Callable, Any, Optional, TYPE_CHECKING

from maspy import Channel, Belief, Goal, Admin

if TYPE_CHECKING:
    from maspy import Agent


# =============================================================================
# FIPA-ACL — Message envelope
# =============================================================================

# Conjunto mínimo de performativas FIPA suportadas
FIPA_PERFORMATIVES = {
    "inform",           # remetente afirma que uma proposição é verdadeira
    "request",          # remetente pede ao receptor que execute uma ação
    "propose",          # remetente submete uma proposta (ex.: lance de leilão)
    "accept-proposal",  # remetente aceita uma proposta recebida anteriormente
    "reject-proposal",  # remetente rejeita uma proposta recebida anteriormente
    "cfp",              # call for proposal — inicia o Contract Net Protocol
    "agree",            # remetente concorda em executar a ação solicitada
    "failure",          # remetente reporta que falhou ao executar uma ação
}

# Níveis de prioridade: valor usado como primeiro elemento na PriorityQueue
FIPA_PRIORITIES: Dict[int, str] = {
    0: "urgent",
    1: "normal",
    2: "low",
}


class FIPAMessage:
    """Envelope de mensagem em conformidade com FIPA-ACL.

    Encapsula todos os campos definidos pela especificação FIPA Agent
    Communication Language. Valida os campos obrigatórios na construção
    e levanta ValueError com uma mensagem descritiva em caso de falha.

    Parâmetros
    ----------
    performative : str
        Ato comunicativo. Deve ser um dos valores de FIPA_PERFORMATIVES.
    sender : str
        Identificador do agente remetente (ex.: "agent_1@maspy").
    receiver : str
        Identificador do agente destinatário (ex.: "hub@spade").
    content : str
        Conteúdo da mensagem como texto livre, expressão FIPA-SL0 ou
        string serializada em JSON.
    priority : int, opcional
        Nível de urgência usado pela fila de prioridade: 0=urgente,
        1=normal (padrão), 2=baixa.
    ontology : str, opcional
        Ontologia que identifica o domínio do conteúdo (ex.: "logistics").
    language : str, opcional
        Descritor da linguagem do conteúdo. Padrão "fipa-sl0".
    conversation_id : str, opcional
        Identificador único da conversa. Gerado automaticamente via UUID4
        se omitido.
    reply_by : str ou None, opcional
        Prazo para resposta no formato ISO 8601
        (ex.: "2026-05-14T10:00:00Z").
    in_reply_to : str ou None, opcional
        conversation_id da mensagem à qual esta responde.

    Exemplos
    --------
    >>> msg = FIPAMessage(
    ...     performative = "inform",
    ...     sender       = "agent_1@maspy",
    ...     receiver     = "hub@spade",
    ...     content      = 'delivery_done("A")',
    ...     priority     = 0
    ... )
    >>> payload = msg.to_dict()          # serializa para transporte HTTP
    >>> msg2 = FIPAMessage.from_dict(payload)  # reconstrói na recepção
    """

    def __init__(
        self,
        performative:    str,
        sender:          str,
        receiver:        str,
        content:         str,
        priority:        int           = 1,
        ontology:        str           = "",
        language:        str           = "fipa-sl0",
        conversation_id: str           = "",
        reply_by:        Optional[str] = None,
        in_reply_to:     Optional[str] = None,
    ) -> None:
        # Campos obrigatórios
        self.performative    = performative
        self.sender          = sender
        self.receiver        = receiver
        self.content         = content

        # Campos opcionais
        self.priority        = priority
        self.ontology        = ontology
        self.language        = language
        self.conversation_id = conversation_id or str(uuid.uuid4())
        self.reply_by        = reply_by
        self.in_reply_to     = in_reply_to

        # Timestamp de criação (UTC, ISO 8601)
        self.timestamp = datetime.now(timezone.utc).isoformat()

        self._validate()

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate(self) -> None:
        """Valida todos os campos conforme a especificação FIPA-ACL.

        Levanta
        -------
        ValueError
            Se um ou mais campos forem inválidos. A mensagem da exceção
            lista todos os erros detectados.
        """
        errors: list[str] = []

        # performative
        if not self.performative:
            errors.append("'performative' is required.")
        elif self.performative not in FIPA_PERFORMATIVES:
            supported = ", ".join(sorted(FIPA_PERFORMATIVES))
            errors.append(
                f"Performative '{self.performative}' is not supported. "
                f"Valid values: {supported}."
            )

        # sender
        if not self.sender or not self.sender.strip():
            errors.append("'sender' is required and must not be empty.")

        # receiver
        if not self.receiver or not self.receiver.strip():
            errors.append("'receiver' is required and must not be empty.")

        # content
        if self.content is None:
            errors.append("'content' is required (may be an empty string).")

        # priority
        if self.priority not in FIPA_PRIORITIES:
            levels = ", ".join(f"{k}={v}" for k, v in FIPA_PRIORITIES.items())
            errors.append(
                f"'priority' must be one of: {levels}. "
                f"Received: {self.priority}."
            )

        # reply_by — deve ser uma string ISO 8601 válida quando fornecida
        if self.reply_by:
            try:
                datetime.fromisoformat(self.reply_by.replace("Z", "+00:00"))
            except ValueError:
                errors.append(
                    f"'reply_by' must be ISO 8601 "
                    f"(e.g. '2026-05-14T10:00:00Z'). Received: {self.reply_by}."
                )

        if errors:
            raise ValueError(
                "Invalid FIPAMessage:\n" +
                "\n".join(f"  - {e}" for e in errors)
            )

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """Serializa para um dicionário compatível com JSON.

        Campos opcionais com valor vazio ou None são omitidos da saída
        para manter o payload compacto.

        Retorna
        -------
        dict
            Representação em dicionário adequada para transporte JSON.
        """
        d: dict = {
            "performative":    self.performative,
            "sender":          self.sender,
            "receiver":        self.receiver,
            "content":         self.content,
            "priority":        self.priority,
            "language":        self.language,
            "conversation_id": self.conversation_id,
            "timestamp":       self.timestamp,
        }
        if self.ontology:
            d["ontology"]    = self.ontology
        if self.reply_by:
            d["reply_by"]    = self.reply_by
        if self.in_reply_to:
            d["in_reply_to"] = self.in_reply_to
        return d

    def to_json(self) -> str:
        """Serializa para uma string JSON formatada.

        Retorna
        -------
        str
            Representação JSON com indentação.
        """
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------
    # Deserialization
    # ------------------------------------------------------------------

    @classmethod
    def from_dict(cls, data: dict) -> "FIPAMessage":
        """Reconstrói uma FIPAMessage a partir de um dicionário.

        Parâmetros
        ----------
        data : dict
            Dicionário contendo ao menos os quatro campos obrigatórios.

        Retorna
        -------
        FIPAMessage
            Uma instância de mensagem validada.

        Levanta
        -------
        ValueError
            Se algum campo obrigatório estiver ausente ou se algum campo
            falhar na validação FIPA.

        Exemplos
        --------
        >>> msg = FIPAMessage.from_dict(received_payload)
        """
        mandatory = ["performative", "sender", "receiver", "content"]
        missing   = [f for f in mandatory if f not in data]
        if missing:
            raise ValueError(
                f"FIPAMessage.from_dict: missing mandatory fields: "
                f"{', '.join(missing)}"
            )
        return cls(
            performative    = data["performative"],
            sender          = data["sender"],
            receiver        = data["receiver"],
            content         = data["content"],
            priority        = data.get("priority", 1),
            ontology        = data.get("ontology", ""),
            language        = data.get("language", "fipa-sl0"),
            conversation_id = data.get("conversation_id", ""),
            reply_by        = data.get("reply_by"),
            in_reply_to     = data.get("in_reply_to"),
        )

    @classmethod
    def from_json(cls, json_str: str) -> "FIPAMessage":
        """Reconstrói uma FIPAMessage a partir de uma string JSON.

        Parâmetros
        ----------
        json_str : str
            Mensagem codificada em JSON.

        Retorna
        -------
        FIPAMessage
            Uma instância de mensagem validada.
        """
        return cls.from_dict(json.loads(json_str))

    # ------------------------------------------------------------------
    # Representation
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        priority_label = FIPA_PRIORITIES.get(self.priority, "?")
        # Normaliza content para string independentemente do tipo real,
        # de modo que o fatiamento nunca levante TypeError com content dict.
        content_str = (
            json.dumps(self.content, ensure_ascii=False)
            if isinstance(self.content, dict)
            else str(self.content)
        )
        truncated = content_str[:40]
        ellipsis  = "..." if len(content_str) > 40 else ""
        return (
            f"FIPAMessage("
            f"performative={self.performative!r}, "
            f"sender={self.sender!r}, "
            f"receiver={self.receiver!r}, "
            f"priority={self.priority}({priority_label}), "
            f"content={truncated!r}{ellipsis})"
        )

    def __str__(self) -> str:
        return self.to_json()


# =============================================================================
# Adaptadores de formato de saída — traduzem uma FIPAMessage para o formato
# de transmissão esperado por cada plataforma de destino.
#
# A FIPAMessage é o formato base: os agentes sempre produzem uma FIPAMessage,
# e os adaptadores a traduzem para o dialeto que cada destino entende no
# momento do envio. Adicionar suporte a uma nova plataforma significa criar
# uma nova classe de adaptador — sem alterar notify() ou _async_post().
#
# Este é o padrão Strategy aplicado à formatação de mensagens de saída.
# =============================================================================

class OutboundAdapter:
    """Classe base para adaptadores de formato de saída.

    Um adaptador recebe uma FIPAMessage (ou um dict simples, por
    compatibilidade retroativa) e retorna um dict serializável em JSON,
    pronto para ser enviado via HTTP por _async_post().
    """

    name: str = "base"

    def format(self, payload: "FIPAMessage | dict") -> dict:
        raise NotImplementedError


class FipaJsonAdapter(OutboundAdapter):
    """Adaptador padrão — serializa a FIPAMessage como JSON FIPA puro.

    É o formato usado pelo SPADE e o comportamento histórico de
    notify(). Um dict simples é repassado sem alteração, por
    compatibilidade retroativa.
    """

    name = "fipa"

    def format(self, payload: "FIPAMessage | dict") -> dict:
        if isinstance(payload, FIPAMessage):
            return payload.to_dict()
        return payload


class JacamoRestAdapter(OutboundAdapter):
    """Adaptador para o endpoint jacamo-rest do JaCaMo.

    O JaCaMo não entende uma FIPAMessage crua. Ele espera uma mensagem
    cujo conteúdo seja um literal Jason que case com um gatilho de plano,
    como ``+mensagem_externa("inform", entrega_concluida(...))``. Este
    adaptador encapsula a performativa e o conteúdo da FIPAMessage nesse
    literal.
    """

    name = "jacamo-rest"

    def format(self, payload: "FIPAMessage | dict") -> dict:
        if isinstance(payload, FIPAMessage):
            performative = payload.performative
            sender       = payload.sender
            content      = payload.content
        else:
            performative = payload.get("performative", "inform")
            sender       = payload.get("sender", "maspy")
            content      = payload.get("content", "")

        return {
            "performative": "tell",
            "sender":       sender,
            "content":      f'mensagem_externa("{performative}", {content})',
        }


# Registro dos adaptadores disponíveis, indexados pelo nome público.
# notify(formato=...) seleciona a partir deste dict. Para adicionar uma
# nova plataforma, defina uma subclasse de OutboundAdapter e registre-a aqui.
OUTBOUND_ADAPTERS: Dict[str, OutboundAdapter] = {
    FipaJsonAdapter.name:   FipaJsonAdapter(),
    JacamoRestAdapter.name: JacamoRestAdapter(),
}


# =============================================================================
# Route — mapeamento de endpoint HTTP para Belief/Goal
# =============================================================================

class Route:
    """Mapeia um endpoint HTTP para um Belief ou Goal do MASPY.

    Quando o ExternalChannel recebe uma requisição POST em um caminho
    registrado, ele usa a Route correspondente para construir o Belief
    ou Goal que será injetado no(s) agente(s) de destino.

    Parâmetros
    ----------
    path : str
        Caminho HTTP (ex.: "/delivery").
    belief : str ou None
        Nome do Belief a injetar. Mutuamente exclusivo com goal.
    goal : str ou None
        Nome do Goal a injetar. Mutuamente exclusivo com belief.
    target : str ou None
        Nome do agente a receber a injeção. Se None, todos os agentes
        conectados recebem a mensagem.
    transform : Callable ou None
        Função ``dict -> tuple`` que extrai valores do payload recebido
        antes de construir o Belief ou Goal. Se None, os valores do
        dicionário são usados na ordem de inserção.

    Levanta
    -------
    ValueError
        Se nem belief nem goal forem fornecidos, ou se ambos forem.
    """

    def __init__(
        self,
        path:      str,
        belief:    Optional[str]      = None,
        goal:      Optional[str]      = None,
        target:    Optional[str]      = None,
        transform: Optional[Callable] = None,
    ) -> None:
        if belief is None and goal is None:
            raise ValueError(f"Route '{path}' requires either belief or goal.")
        if belief is not None and goal is not None:
            raise ValueError(f"Route '{path}' must specify belief OR goal, not both.")

        self.path      = path
        self.belief    = belief
        self.goal      = goal
        self.target    = target
        self.transform = transform

    def build(self, payload: dict) -> "Belief | Goal":
        """Constrói o Belief ou Goal a partir de um payload HTTP recebido.

        Quando o payload é uma FIPAMessage serializada, o campo content
        é extraído e desserializado antes de ser passado à função
        transform. O conteúdo pode chegar como string JSON ou como dict
        já desserializado, dependendo do cliente HTTP; ambos os casos são
        tratados de forma transparente.

        Parâmetros
        ----------
        payload : dict
            Payload JSON cru da requisição HTTP. Se contiver os campos
            ``performative`` e ``sender``, é tratado como uma FIPAMessage
            e seu ``content`` é extraído primeiro.

        Retorna
        -------
        Belief ou Goal
            A estrutura de dados BDI pronta para ser injetada no agente.

        Levanta
        -------
        ValueError
            Se o payload for uma FIPAMessage cujo content não pode ser
            interpretado, ou se a função transform levantar um erro.
            Levantar aqui faz o servidor HTTP retornar 400/500 em vez de
            injetar silenciosamente uma mensagem corrompida.
        """
        # Quando o payload carrega campos FIPA, extrai o content
        # e o usa como entrada para a função transform.
        if "performative" in payload and "content" in payload:
            # A validação da FIPAMessage já ocorreu em handle_post;
            # from_dict aqui serve apenas para acessar o campo content.
            msg         = FIPAMessage.from_dict(payload)
            content_raw = msg.content

            # O conteúdo pode chegar como string JSON ou como dict,
            # dependendo de o cliente HTTP tê-lo pré-interpretado ou não.
            if isinstance(content_raw, str):
                try:
                    content_data = json.loads(content_raw)
                except (json.JSONDecodeError, TypeError):
                    content_data = None
            elif isinstance(content_raw, dict):
                content_data = content_raw
            else:
                content_data = None

            # Usa o dict de content extraído quando disponível; recorre ao
            # payload completo apenas quando o content não é um dict
            # estruturado (ex.: string simples como 'delivery_done("A")').
            data = content_data if isinstance(content_data, dict) else payload
        else:
            data = payload

        try:
            values = self.transform(data) if self.transform else tuple(data.values())
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"Route '{self.path}': transform failed on payload "
                f"{data!r} — {exc}. Check that the transform function "
                f"matches the expected content structure."
            ) from exc

        if self.belief:
            return Belief(self.belief, values)
        return Goal(self.goal, values)  # type: ignore[arg-type]


# =============================================================================
# ExternalChannel — Channel do MASPY com suporte a HTTP
# =============================================================================

class ExternalChannel(Channel):
    """Channel do MASPY com conectividade HTTP externa e suporte a FIPA-ACL.

    Estende Channel com três capacidades:

    1. **Servidor HTTP** (aiohttp): aceita requisições POST de sistemas
       externos, valida envelopes FIPA-ACL e injeta os Beliefs ou Goals
       resultantes nos agentes conectados via o mecanismo nativo
       ``agent.add()``.

    2. **Fila de prioridade**: mensagens recebidas são bufferizadas em
       uma ``PriorityQueue`` e consumidas por uma thread de trabalho
       dedicada. Mensagens em prioridade 0 (urgente) são sempre
       processadas antes das de prioridade 1 (normal) e 2 (baixa);
       empates no mesmo nível são resolvidos pela ordem de chegada (FIFO).

    3. **notify() não bloqueante**: chamadas HTTP de saída são submetidas
       a um event loop asyncio dedicado e retornam imediatamente,
       deixando o ciclo BDI do agente desbloqueado. Isso espelha a
       semântica de envio fire-and-forget de SPADE e JADE.

    O framework MASPY em si não é modificado. Os agentes conectados usam
    a API nativa (``@pl``, ``Belief``, ``Goal``, ``self.send()``) sem
    qualquer ciência da camada de conectividade externa.

    Notas
    -----
    **Use** ``ExternalChannel.create()`` **em vez do construtor
    diretamente.** A classe base Channel usa uma metaclasse Multiton
    (``CommsMultiton``) que intercepta ``__call__`` e aceita apenas o
    nome do canal. Passar kwargs extras (como ``port``) levanta um
    ``TypeError``. ``create()`` contorna isso armazenando a porta no dict
    de nível de classe ``_pending_ports`` antes da instanciação;
    ``__init__`` a lê e a remove com ``pop()``.
    """

    # Registros de nível de classe usados para passar port e api_key
    # através da barreira do Multiton: create() escreve aqui, __init__ lê com pop().
    _pending_ports:   Dict[str, int]           = {}
    _pending_api_keys: Dict[str, Optional[str]] = {}

    @classmethod
    def create(
        cls,
        comm_name: str           = "external",
        port:      int           = 9000,
        api_key:   Optional[str] = None,
    ) -> "ExternalChannel":
        """Método fábrica — o ponto de entrada correto para instanciação.

        Registra a porta em ``_pending_ports`` para que ``__init__`` possa
        recuperá-la depois que o Multiton chamou o construtor apenas com
        o nome do canal.

        Parâmetros
        ----------
        comm_name : str, opcional
            Nome do canal, único por aplicação. Padrão "external".
        port : int, opcional
            Porta TCP em que o servidor HTTP irá escutar. Padrão 9000.
        api_key : str ou None, opcional
            Bearer token opcional para autenticação das requisições de
            entrada. Quando definido, todo POST deve incluir o cabeçalho
            ``Authorization: Bearer <api_key>``; requisições sem ele
            recebem HTTP 401. Quando None (padrão), a autenticação fica
            desativada — adequado para redes locais confiáveis e
            ambientes de prova de conceito.

        Retorna
        -------
        ExternalChannel
            A instância de canal (possivelmente em cache).

        Exemplos
        --------
        >>> channel = ExternalChannel.create("delivery_channel", port=9000)
        >>> # Com autenticação:
        >>> channel = ExternalChannel.create("secure_channel", port=9000,
        ...                                  api_key="secret-token")
        """
        cls._pending_ports[comm_name]    = port
        cls._pending_api_keys[comm_name] = api_key
        return cls(comm_name)

    def __init__(self, comm_name: str = "external") -> None:
        # Recupera a porta registrada por create(); usa 9000 como padrão
        # se __init__ for chamado sem create() por algum motivo (defensivo).
        self._port:    int           = ExternalChannel._pending_ports.pop(comm_name, 9000)
        self._api_key: Optional[str] = ExternalChannel._pending_api_keys.pop(comm_name, None)
        self._routes:  Dict[str, Route] = {}

        # Fila de prioridade — layout da tupla: (priority, sequence, agent_key, data)
        #   priority  — 0=urgente, 1=normal, 2=baixa
        #   sequence  — contador monotonicamente crescente (desempate FIFO)
        #   agent_key — nome do agente de destino, ou None para broadcast
        #   data      — instância de Belief ou Goal a injetar
        self._priority_queue: queue.PriorityQueue = queue.PriorityQueue()
        self._sequence    = itertools.count()   # thread-safe via GIL
        self._worker_stop = threading.Event()   # sinaliza a saída do worker

        # Referência ao AppRunner do aiohttp — guardada para que stop() possa
        # chamar cleanup() e liberar a porta TCP de forma limpa (evita TIME_WAIT no restart).
        self._runner:  Optional[web.AppRunner]        = None

        # Sessão HTTP de saída persistente — reutilizada em todas as chamadas
        # de notify() para aproveitar o pool de conexões (evita abrir uma nova
        # conexão TCP a cada mensagem de saída, espelhando o padrão do SPADE
        # de uma sessão de transporte longeva por agente).
        self._session: Optional[aiohttp.ClientSession] = None

        # Event loop asyncio dedicado, rodando em uma thread daemon.
        # Necessário porque o MASPY usa threading e event loops do asyncio
        # não podem ser compartilhados entre threads. A ponte entre os dois
        # modelos de concorrência é asyncio.run_coroutine_threadsafe().
        self._loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever,
            daemon=True,
            name=f"ExternalChannel-{comm_name}-Loop"
        )
        self._loop_thread.start()

        # Inicializa o Channel pai — preserva todo o comportamento nativo
        super().__init__(comm_name)

        self._start_http_server()
        self._start_worker()
        self.print(f"ExternalChannel '{comm_name}' active at http://0.0.0.0:{self._port}")

    # ------------------------------------------------------------------
    # Route registration
    # ------------------------------------------------------------------

    def route(
        self,
        path:      str,
        belief:    Optional[str]      = None,
        goal:      Optional[str]      = None,
        target:    Optional[str]      = None,
        transform: Optional[Callable] = None,
    ) -> "ExternalChannel":
        """Registra uma rota HTTP que injeta um Belief ou Goal nos agentes.

        Retorna ``self`` para permitir encadeamento de métodos.

        Parâmetros
        ----------
        path : str
            Caminho HTTP a registrar (ex.: "/delivery").
        belief : str ou None
            Nome do Belief a criar na chegada. Mutuamente exclusivo
            com ``goal``.
        goal : str ou None
            Nome do Goal a criar na chegada. Mutuamente exclusivo
            com ``belief``.
        target : str ou None
            Agente a receber a injeção. Se None, todos os agentes
            conectados recebem a mensagem.
        transform : Callable ou None
            Função ``dict -> tuple`` aplicada ao conteúdo extraído antes
            de construir o Belief ou Goal.

        Retorna
        -------
        ExternalChannel
            Esta instância, habilitando ``channel.route(...).route(...)``.

        Exemplos
        --------
        >>> channel.route("/delivery", belief="delivery_order",
        ...               transform=lambda p: (p["carrier"], p["cep"], p["cost"]))
        >>> channel.route("/alert", goal="check_alert")
        """
        r = Route(path, belief=belief, goal=goal, target=target, transform=transform)
        self._routes[path] = r
        self.print(
            f"Route registered: POST {path} → "
            f"{'Belief' if r.belief else 'Goal'}({r.belief or r.goal})"
        )
        return self

    # ------------------------------------------------------------------
    # Outbound messaging
    # ------------------------------------------------------------------

    def notify(
        self,
        url:     str,
        payload: "FIPAMessage | dict",
        method:  str = "POST",
        formato: str = "fipa",
    ) -> "Optional[concurrent.futures.Future]":
        """Envia uma mensagem HTTP a um sistema externo de forma assíncrona.

        Segue o padrão fire-and-forget usado por SPADE e JADE: o agente
        chamador não é bloqueado enquanto aguarda a resposta HTTP. A
        requisição é submetida ao loop asyncio interno e a entrega ocorre
        em segundo plano.

        Retorna o ``concurrent.futures.Future`` subjacente, de modo que o
        chamador possa, opcionalmente, aguardar a conclusão ou verificar
        erros — útil para testes automatizados e medições de latência sem
        forçar comportamento síncrono nos agentes de produção.

        Se a requisição falhar (conexão recusada, timeout, HTTP 4xx/5xx),
        o erro é registrado pela fila de print do canal. Para confirmação
        de entrega, registre um plano reativo que trate a resposta
        ``inform`` esperada.

        Parâmetros
        ----------
        url : str
            URL de destino completa (ex.: "http://localhost:5000/inbox").
        payload : FIPAMessage ou dict
            Mensagem a enviar. FIPAMessage é recomendada para
            interoperabilidade; dicts simples são aceitos por
            compatibilidade retroativa.
        method : str, opcional
            Método HTTP. Padrão "POST".
        formato : str, opcional
            Formato de saída que seleciona qual adaptador traduz a
            FIPAMessage antes do envio. Padrão "fipa" (JSON FIPA puro,
            usado pelo SPADE). Use "jacamo-rest" para mirar um endpoint
            jacamo-rest do JaCaMo. Levanta ValueError se o formato não
            estiver registrado em OUTBOUND_ADAPTERS.

        Retorna
        -------
        concurrent.futures.Future ou None
            O future que representa a tarefa de entrega assíncrona.
            Retorna None se o payload falhar na validação FIPA.
            Agentes podem ignorar o valor de retorno com segurança;
            testes podem chamar ``future.result(timeout=5)`` para
            confirmar a entrega.

        Exemplos
        --------
        >>> # Uso normal — fire-and-forget
        >>> channel.notify("http://localhost:5000/inbox", msg)

        >>> # Uso em teste — aguarda confirmação de entrega
        >>> future = channel.notify("http://localhost:5000/inbox", msg)
        >>> if future:
        ...     future.result(timeout=5)
        """
        # Seleciona o adaptador de saída pelo formato. Formato desconhecido
        # é erro de programação — falha explicitamente com as opções válidas.
        adapter = OUTBOUND_ADAPTERS.get(formato)
        if adapter is None:
            disponiveis = ", ".join(sorted(OUTBOUND_ADAPTERS.keys()))
            raise ValueError(
                f"Unknown notify format '{formato}'. "
                f"Available formats: {disponiveis}."
            )

        try:
            # O adaptador traduz a FIPAMessage (formato base) para o
            # formato de transmissão esperado pela plataforma de destino.
            data = adapter.format(payload)
            self.print(f"notify [{formato}] → {url}")

            # Submete a corrotina ao loop asyncio; retorna imediatamente.
            # O Future retornado permite rastrear a entrega quando necessário.
            return asyncio.run_coroutine_threadsafe(
                self._async_post(url, data, method),
                self._loop
            )
        except ValueError as e:
            self.print(f"Invalid FIPAMessage: {e}")
            return None

    async def _async_post(self, url: str, data: dict, method: str) -> None:
        """Executa uma requisição HTTP de saída dentro do event loop asyncio.

        Reutiliza a ``ClientSession`` persistente criada na inicialização
        do servidor para aproveitar o pool de conexões — evita o custo de
        abrir uma nova conexão TCP a cada mensagem de saída.

        Parâmetros
        ----------
        url : str
            URL de destino.
        data : dict
            Payload serializável em JSON.
        method : str
            String do método HTTP (ex.: "POST").
        """
        session = self._session
        if session is None or session.closed:
            session = aiohttp.ClientSession()
            self._session = session
        try:
            async with session.request(
                method, url,
                json=data,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status >= 400:
                    self.print(f"notify: HTTP {resp.status} from {url}")
        except aiohttp.ClientConnectorError:
            self.print(f"notify: connection refused — {url}")
        except asyncio.TimeoutError:
            self.print(f"notify: request timed out — {url}")
        except Exception as exc:
            self.print(f"notify: unexpected error — {url}: {exc}")

    # ------------------------------------------------------------------
    # Channel lookup
    # ------------------------------------------------------------------

    @staticmethod
    def get_channel(name: str) -> Optional["ExternalChannel"]:
        """Recupera uma instância de ExternalChannel em execução pelo nome.

        Usa o registro ``CommsMultiton`` do MASPY, que garante uma única
        instância por nome de canal dentro do processo.

        Parâmetros
        ----------
        name : str
            O nome do canal passado a ``ExternalChannel.create()``.

        Retorna
        -------
        ExternalChannel ou None
            A instância do canal, ou None se não existir um
            ExternalChannel com esse nome.

        Exemplos
        --------
        >>> channel = ExternalChannel.get_channel("delivery_channel")
        >>> channel.notify(url, message)
        """
        from maspy.communication import CommsMultiton
        instance = CommsMultiton.get_instance(name)
        if isinstance(instance, ExternalChannel):
            return instance
        return None

    # ------------------------------------------------------------------
    # Internal HTTP server
    # ------------------------------------------------------------------

    def _start_http_server(self) -> None:
        """Agenda a inicialização do servidor aiohttp no event loop asyncio."""
        asyncio.run_coroutine_threadsafe(
            self._run_aiohttp_server(),
            self._loop
        )

    async def _run_aiohttp_server(self) -> None:
        """Executa o servidor HTTP aiohttp dentro do event loop asyncio.

        Trata todas as requisições recebidas de forma concorrente dentro
        do mesmo event loop que processa as chamadas de notify() de saída,
        eliminando a necessidade de threads adicionais.

        Rotas:
            POST /<caminho-registrado>  Recebe e despacha uma mensagem.
            GET  /info                  Retorna o estado do canal em JSON.
        """
        channel = self
        # Limita o payload de entrada a 1 MB para evitar esgotamento de
        # memória por mensagens inesperadamente grandes.
        app     = web.Application(client_max_size=1024 ** 2)

        async def handle_post(request: web.Request) -> web.Response:
            path = request.path

            # Autenticação por bearer token — aplicada apenas quando api_key
            # está configurada; ignorada por completo no modo aberto (padrão).
            if channel._api_key is not None:
                auth = request.headers.get("Authorization", "")
                if auth != f"Bearer {channel._api_key}":
                    return web.json_response(
                        {"error": "Unauthorized — invalid or missing Bearer token."},
                        status=401
                    )

            if path not in channel._routes:
                return web.json_response(
                    {"error": f"Route '{path}' is not registered."}, status=404
                )
            try:
                payload = await request.json()

                # Extrai a prioridade do envelope FIPA quando presente;
                # recorre a normal (1) para payloads JSON simples.
                priority  = 1
                fipa_info = ""
                if "performative" in payload and "sender" in payload:
                    try:
                        msg       = FIPAMessage.from_dict(payload)
                        priority  = msg.priority
                        fipa_info = (
                            f" [{msg.performative}|p{msg.priority}]"
                            f" {msg.sender}→{msg.receiver}"
                        )
                    except ValueError as exc:
                        return web.json_response(
                            {"error": f"Invalid FIPAMessage: {exc}"}, status=400
                        )

                channel.print(f"POST {path}{fipa_info}")

                route = channel._routes[path]
                data  = route.build(payload)

                # Repassa para a fila de prioridade; _inject() é síncrono
                # e thread-safe via o lock interno da PriorityQueue.
                channel._inject(data, route.target, priority)

                # 202 Accepted é semanticamente correto aqui: a mensagem
                # foi aceita e enfileirada, mas o agente BDI a processará
                # de forma assíncrona — espelhando o comportamento do ACC
                # do JADE e o inbox assíncrono do JaCaMo-Web.
                return web.json_response(
                    {
                        "status":   "accepted",
                        "queued":   True,
                        "priority": priority,
                    },
                    status=202
                )

            except json.JSONDecodeError as exc:
                return web.json_response(
                    {"error": f"Invalid JSON: {exc}"}, status=400
                )
            except Exception as exc:
                channel.print(f"Error handling POST {path}: {exc}")
                return web.json_response({"error": str(exc)}, status=500)

        async def handle_get(request: web.Request) -> web.Response:
            """Retorna o estado atual do canal para inspeção."""
            return web.json_response({
                "channel":            channel.my_name,
                "port":               channel._port,
                "routes":             list(channel._routes.keys()),
                "connected_agents":   list(channel._agents.keys()),
                "fipa_performatives": sorted(FIPA_PERFORMATIVES),
                "priority_levels":    FIPA_PRIORITIES,
                "queue_size":         channel._priority_queue.qsize(),
            })

        app.router.add_post("/{path_info:.*}", handle_post)
        app.router.add_get("/info",            handle_get)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", channel._port)
        await site.start()
        channel._runner = runner
        # Cria a sessão de saída persistente assim que o event loop está
        # rodando — ClientSession deve ser criada dentro de um contexto async.
        channel._session = aiohttp.ClientSession()
        self.print(f"HTTP server started at http://0.0.0.0:{channel._port}")

    # ------------------------------------------------------------------
    # Priority queue worker
    # ------------------------------------------------------------------

    def _start_worker(self) -> None:
        """Inicia a thread daemon que drena a fila de prioridade."""
        self._worker_thread = threading.Thread(
            target=self._worker,
            daemon=True,
            name=f"ExternalChannel-{self.my_name}-Worker"
        )
        self._worker_thread.start()

    def _worker(self) -> None:
        """Consome a fila de prioridade e injeta as mensagens nos agentes.

        Roda em sua própria thread daemon. As mensagens são entregues em
        ordem de prioridade (0 primeiro), com ordenação FIFO dentro do
        mesmo nível de prioridade.

        ``agent.add()`` é chamado diretamente — o ciclo BDI do MASPY já
        gerencia internamente seu próprio ``update_lock``; adquiri-lo a
        partir de uma thread externa causa deadlock, pois o ciclo mantém
        o lock durante ``_perception()`` e ``_mail()``. O GIL e o lock
        interno da ``PriorityQueue`` fornecem proteção suficiente para as
        operações de lista dentro de ``add()``.

        Encerra de forma limpa quando ``_worker_stop`` é setado.
        """
        while not self._worker_stop.is_set():
            try:
                # Um timeout curto permite ao laço checar _worker_stop
                # periodicamente sem busy-waiting.
                priority, seq, agent_key, data = self._priority_queue.get(timeout=0.1)
                priority_label = FIPA_PRIORITIES.get(priority, "?")

                if agent_key:
                    if agent_key in self._agents:
                        self._agents[agent_key].add(data)
                        self.print(
                            f"[queue p{priority}/{priority_label}] "
                            f"Injected into '{agent_key}': {data}"
                        )
                    else:
                        self.print(
                            f"[queue] Agent '{agent_key}' not found in channel."
                        )
                else:
                    for key, agent in self._agents.items():
                        agent.add(data)
                    self.print(
                        f"[queue p{priority}/{priority_label}] "
                        f"Broadcast to all agents: {data}"
                    )
                self._priority_queue.task_done()

            except queue.Empty:
                continue  # timeout normal — reavalia a flag de parada
            except Exception as exc:
                self.print(f"[queue] Worker error: {exc}")

    def _inject(
        self,
        data:     "Belief | Goal",
        target:   Optional[str],
        priority: int = 1,
    ) -> None:
        """Enfileira um Belief ou Goal para entrega pela thread de trabalho.

        Este método é chamado de dentro do event loop asyncio e é seguro
        usar entre threads porque a ``PriorityQueue`` é protegida por lock
        internamente.

        Parâmetros
        ----------
        data : Belief ou Goal
            A estrutura de dados BDI a injetar.
        target : str ou None
            Nome do agente de destino. Se None, a mensagem é enviada em
            broadcast para todos os agentes conectados a este canal.
            A busca tenta primeiro uma correspondência exata de chave; se
            não encontrar, busca por prefixo de nome (ex.: "vehicle" casa
            com "vehicle_1", "vehicle_2" etc.) e entrega à primeira
            correspondência. Isso evita perda silenciosa de mensagens
            quando o chamador usa o nome base sem o sufixo numérico do MASPY.
        priority : int, opcional
            Prioridade de entrega (0=urgente, 1=normal, 2=baixa).
            Padrão 1.
        """
        sequence  = next(self._sequence)
        agent_key = None

        if target:
            agent_key = self._resolve_agent_key(target)
            if agent_key is None:
                self.print(
                    f"[inject] No agent matching '{target}' found in channel "
                    f"(connected: {list(self._agents.keys())})."
                )
                return
        else:
            # Aviso explícito de broadcast — ajuda o desenvolvedor a
            # detectar fan-out acidental quando um alvo específico era a intenção.
            if self._agents:
                self.print(
                    f"[inject] Broadcasting to all {len(self._agents)} "
                    f"connected agent(s): {list(self._agents.keys())}."
                )

        self._priority_queue.put((priority, sequence, agent_key, data))
        priority_label = FIPA_PRIORITIES.get(priority, "?")
        self.print(
            f"Queued p{priority}/{priority_label} "
            f"(queue_size={self._priority_queue.qsize()}): {data}"
        )

    def _resolve_agent_key(self, target: str) -> Optional[str]:
        """Resolve um nome de alvo para uma chave real em ``self._agents``.

        Ordem de resolução:
        1. Correspondência exata — ``target`` já é uma chave válida.
        2. Sufixo — ``target + "_1"`` cobre a convenção comum do MASPY em
           que ``Agent("name")`` é registrado como ``name_1``.
        3. Busca por prefixo — coleta todas as chaves cujo nome base é
           igual a ``target`` (ex.: "vehicle" casa com "vehicle_1",
           "vehicle_2"). Retorna a correspondência única ou levanta
           ``ValueError`` se houver múltiplos candidatos — espelhando a
           resolução estrita de AID do JADE, que rejeita identificadores
           ambíguos.

        Retorna ``None`` se nenhuma correspondência for encontrada.

        Parâmetros
        ----------
        target : str
            Nome do agente conforme fornecido pela rota ou pelo chamador.

        Retorna
        -------
        str ou None
            A chave correspondente em ``self._agents``, ou None.

        Levanta
        -------
        ValueError
            Se ``target`` casar com mais de um agente por prefixo,
            obrigando o chamador a usar um nome não ambíguo.
        """
        # 1. Correspondência exata
        if target in self._agents:
            return target

        # 2. Acrescenta o sufixo numérico padrão do MASPY
        candidate = f"{target}_1"
        if candidate in self._agents:
            return candidate

        # 3. Busca por prefixo — estrita: levanta erro em caso de ambiguidade
        matches = [
            key for key in self._agents
            if ("_".join(key.split("_")[:-1]) if "_" in key else key) == target
        ]

        if len(matches) == 1:
            return matches[0]

        if len(matches) > 1:
            raise ValueError(
                f"Ambiguous target '{target}': matches multiple agents "
                f"{matches}. Use an unambiguous name."
            )

        return None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def stop(self) -> None:
        """Encerra o servidor HTTP, a thread de trabalho e o loop asyncio.

        Executa uma sequência de encerramento graciosa:
        1. Sinaliza à thread de trabalho que pare de drenar a fila.
        2. Chama ``runner.cleanup()`` dentro do loop asyncio para que o
           aiohttp feche todas as conexões ativas e libere a porta TCP de
           forma limpa — evitando ``OSError: Address already in use`` em
           reinício imediato.
        3. Para o event loop asyncio.
        """
        self._worker_stop.set()

        async def _async_stop() -> None:
            if self._session is not None and not self._session.closed:
                await self._session.close()
            if self._runner is not None:
                await self._runner.cleanup()

        # Submete a corrotina de limpeza e aguarda sua conclusão antes de
        # parar o loop, para que a porta seja liberada antes de retornarmos.
        if self._loop.is_running():
            future = asyncio.run_coroutine_threadsafe(_async_stop(), self._loop)
            try:
                future.result(timeout=5)
            except Exception as exc:
                self.print(f"Warning during HTTP cleanup: {exc}")
            finally:
                self._loop.call_soon_threadsafe(self._loop.stop)

        self.print("ExternalChannel stopped.")
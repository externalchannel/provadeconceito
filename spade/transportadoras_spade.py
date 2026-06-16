import json
import random
import asyncio
import aiohttp
import sys
from aiohttp import web
import spade
from spade.agent import Agent

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# ══════════════════════════════════════════════════════════════════════════════
# Tabela de regiões brasileiras
# ══════════════════════════════════════════════════════════════════════════════
REGIOES = {
    "Norte":        {"AM","PA","RR","RO","AC","AP","TO"},
    "Nordeste":     {"MA","PI","CE","RN","PB","PE","AL","SE","BA"},
    "Centro-Oeste": {"MT","MS","GO","DF"},
    "Sudeste":      {"SP","RJ","MG","ES"},
    "Sul":          {"PR","SC","RS"},
}
FATOR_REGIAO = {
    "Sul": 1.0, "Sudeste": 1.2, "Centro-Oeste": 1.5,
    "Nordeste": 1.8, "Norte": 2.2,
}
CUSTO_BASE = {"A": 30, "B": 35, "C": 40}

def determinar_regiao(estado):
    for regiao, estados in REGIOES.items():
        if estado.upper() in estados:
            return regiao
    return "Sudeste"

def calcular_custo(transportadora, estado):
    regiao   = determinar_regiao(estado)
    base     = CUSTO_BASE.get(transportadora, 30)
    fator    = FATOR_REGIAO.get(regiao, 1.0)
    variacao = random.uniform(0.9, 1.1)
    return round(base * fator * variacao, 2)

# ══════════════════════════════════════════════════════════════════════════════
# Configurações de rede
# ══════════════════════════════════════════════════════════════════════════════
IP_JACAMO       = "10.142.227.96"   # host do JaCaMo na rede local — ajustar ao seu ambiente
PORTA_JACAMO    = "8080"
URL_BASE_JACAMO = f"http://{IP_JACAMO}:{PORTA_JACAMO}"

IP_MASPY        = "localhost"
PORTA_MASPY     = "9000"
URL_BASE_MASPY  = f"http://{IP_MASPY}:{PORTA_MASPY}"

AID_SPADE = "hub@spade"


# ══════════════════════════════════════════════════════════════════════════════
# Hub de Transportadoras
# ══════════════════════════════════════════════════════════════════════════════
class HubTransportadoras(Agent):

    # ── Comunicação com JaCaMo (formato jacamo-rest — não muda) ──────────────
    async def enviar_para_jacamo(self, performativa, conteudo, remetente):
        url_jacamo = f"{URL_BASE_JACAMO}/agents/negociador/inbox"
        payload = {
            "performative": "tell",
            "sender":       remetente,
            "content":      f"mensagem_externa(\"{performativa}\", {conteudo})"
        }
        try:
            async with aiohttp.ClientSession() as session:
                resp = await session.post(url_jacamo, json=payload)
                print(f"[SPADE] → JaCaMo: {performativa} | status {resp.status}")
        except Exception as e:
            print(f"[SPADE] ERRO ao falar com JaCaMo ({url_jacamo}): {e}")

    # ── Aciona a frota MASPY via broadcast ────────────────────────────────────
    async def acionar_frota_maspy(self, transportadora, cep, custo):
        """
        Envia FIPAMessage para /entrega sem target específico.
        O ExternalChannel faz broadcast para todos os veículos.
        A frota se auto-organiza internamente via anel.
        """
        url_maspy = f"{URL_BASE_MASPY}/entrega"

        content = json.dumps({
            "transportadora": transportadora,
            "cep":            cep,
            "custo":          custo,
        })

        fipa_payload = {
            "performative": "request",
            "sender":       AID_SPADE,
            "receiver":     "frota@maspy",
            "content":      content,
            "priority":     0,
            "ontology":     "logistica",
            "language":     "fipa-sl0",
        }

        print(f"\n[SPADE] Acionando frota MASPY — broadcast para todos os veículos")
        print(f"[SPADE]   sender       : {AID_SPADE}")
        print(f"[SPADE]   receiver     : frota@maspy (broadcast)")
        print(f"[SPADE]   transportadora: {transportadora} | CEP: {cep} | Custo: R${custo}")
        print(f"[SPADE]   priority     : 0 (urgente)")
        print(f"[SPADE] Aguardando auto-organização da frota...")

        try:
            async with aiohttp.ClientSession() as session:
                resp = await session.post(url_maspy, json=fipa_payload)
                print(f"[SPADE] MASPY respondeu: {resp.status}")
        except Exception as e:
            print(f"[SPADE] Erro ao acionar frota MASPY: {e}")

    # ── Processa mensagens recebidas ──────────────────────────────────────────
    async def processar_mensagem(self, request):
        dados = await request.json()
        fase  = dados.get('performative')

        # ── CFP do JaCaMo ─────────────────────────────────────────────────────
        if fase == "cfp":
            cep    = dados.get('cep', '00000000')
            estado = dados.get('estado', 'SP')
            regiao = determinar_regiao(estado)

            print(f"\n[SPADE] CFP recebido!")
            print(f"[SPADE]   CEP    : {cep}")
            print(f"[SPADE]   Estado : {estado} ({regiao})")
            print(f"[SPADE] Transportadoras calculando lances...")

            self._cep_atual    = cep
            self._estado_atual = estado
            self._propostas    = {}

            prazo_base = {"A": 3, "B": 2, "C": 4}
            for nome in ["A", "B", "C"]:
                custo = calcular_custo(nome, estado)
                prazo = prazo_base[nome] + random.randint(0, 2)
                print(f"  -> [Transp {nome}] "
                      f"Base R${CUSTO_BASE[nome]} "
                      f"x Fator {FATOR_REGIAO[regiao]} ({regiao}) "
                      f"= R${custo} | Prazo: {prazo} dias.")
                self._propostas[nome] = {"custo": custo, "prazo": prazo}
                conteudo = f"proposta(\"{nome}\", {custo}, {prazo})"
                await asyncio.sleep(0.5)
                asyncio.create_task(
                    self.enviar_para_jacamo("propose", conteudo, f"transp_{nome}")
                )

        # ── accept_proposal do JaCaMo ─────────────────────────────────────────
        elif fase == "accept_proposal":
            conteudo  = dados.get('content', '')
            vencedora = conteudo.replace('"', '')
            cep       = self._cep_atual
            estado    = self._estado_atual
            proposta  = self._propostas.get(vencedora, {})
            custo     = proposta.get("custo", 0)
            prazo     = proposta.get("prazo", 0)

            print(f"\n[SPADE] Transportadora {vencedora} venceu!")
            print(f"[SPADE]   Custo : R${custo} | Prazo: {prazo} dias")
            print(f"[SPADE]   Região: {determinar_regiao(estado)}")

            # Aciona a frota MASPY via broadcast
            self._relatorios_recebidos = []
            self._total_veiculos = 3
            await self.acionar_frota_maspy(vencedora, cep, custo)

        # ── inform da frota MASPY — relatório de eleição ──────────────────────
        elif fase == "inform":
            sender  = dados.get('sender', 'desconhecido')
            content = dados.get('content', '{}')
            prio    = dados.get('priority', 1)
            conv_id = dados.get('conversation_id', '')

            # Desserializa o relatório
            try:
                relatorio = json.loads(content) if isinstance(content, str) else content
            except json.JSONDecodeError:
                relatorio = {"raw": content}

            # Relatório do veículo eleito (priority=0)
            if relatorio.get("status") != "recusado":
                print(f"\n[SPADE] ✓ Relatório de eleição recebido!")
                print(f"[SPADE]   Vencedor       : {relatorio.get('vencedor', sender)}")
                print(f"[SPADE]   Distância      : {relatorio.get('distancia_km', '?')} km")
                print(f"[SPADE]   Tempo entrega  : {relatorio.get('tempo_entrega_s', '?')}s")
                print(f"[SPADE]   Transportadora : {relatorio.get('transportadora', '?')}")
                print(f"[SPADE]   CEP            : {relatorio.get('cep', '?')}")
                print(f"[SPADE]   conversation_id: {conv_id}")

                concorrentes = relatorio.get("concorrentes", [])
                if concorrentes:
                    print(f"[SPADE]   Concorrentes recusados:")
                    for c in concorrentes:
                        dist = c.get('distancia_km', 'N/A')
                        print(f"[SPADE]     - {c['veiculo']}: {dist} km")

                # Repassa confirmação ao JaCaMo
                conteudo_jacamo = (
                    f"entrega_concluida(\"{relatorio.get('vencedor', sender)}\", "
                    f"{relatorio.get('distancia_km', 0)}, "
                    f"{relatorio.get('tempo_entrega_s', 0)})"
                )
                await self.enviar_para_jacamo("inform", conteudo_jacamo, sender)

            # Relatório de veículo recusado (priority=1)
            else:
                print(f"[SPADE]   Recusado: {relatorio.get('veiculo', sender)} "
                      f"({relatorio.get('distancia_km', '?')} km)")

        return web.json_response({"status": "ok"})

    async def setup(self):
        self._cep_atual              = "00000000"
        self._estado_atual           = "SP"
        self._propostas              = {}
        self._relatorios_recebidos   = []
        self._total_veiculos         = 3

        print("[SPADE] Hub de Transportadoras iniciado na porta 5000!")
        app = web.Application()
        app.add_routes([web.post('/inbox_spade', self.processar_mensagem)])
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, 'localhost', 5000)
        await asyncio.create_task(site.start())


async def main():
    # Configure com uma conta XMPP válida antes de executar.
    JID_AGENTE   = "ponte_jacamo_hub_2026@yax.im"
    SENHA_AGENTE = "<defina_a_senha_do_agente>"
    agente = HubTransportadoras(JID_AGENTE, SENHA_AGENTE)
    await agente.start()
    while agente.is_alive():
        await asyncio.sleep(1)


if __name__ == "__main__":
    spade.run(main())
"""
veiculo_maspy.py
================
Frota autônoma de entrega — Cenário 3: auto-organização em anel
Com coleta de métricas via MetricsCollector (separado do ExternalChannel)

Canais:
  ExternalChannel (HTTP/FIPA-ACL) — SPADE <-> MASPY
  frota_interna   (Channel nativo) — veículos <-> veículos

Métricas (MetricsCollector — separado do canal):
  G1 — Latência de injeção (POST -> agent.add)
  G2 — Tempo de eleição + mensagens no anel
  G3 — Taxa de acerto + qualidade da decisão
  G4 — Latência end-to-end
"""

import json
import time
import random
from maspy import *
from external_channel import ExternalChannel, FIPAMessage
from metrics_collector import MetricsCollector

# ── Configuração ──────────────────────────────────────────────────────────────
PORTA        = 9000
URL_SPADE    = "http://localhost:5000/inbox_spade"
AID_SPADE    = "hub@spade"
CANAL_FROTA  = "frota_interna"
FATOR_TEMPO  = 2.0

# JaCaMo — rastreamento direto do veículo (Opção D)
# O veículo eleito envia eventos de rastreamento direto ao JaCaMo,
# usando o formato jacamo-rest. Espelha um app de delivery em tempo real:
# a confirmação comercial vem do SPADE, o rastreamento vem do veículo.
IP_JACAMO    = "10.142.227.96"   # host do JaCaMo na rede local — ajustar ao seu ambiente
URL_JACAMO   = f"http://{IP_JACAMO}:8080/agents/negociador/inbox"
AID_JACAMO   = "negociador@jacamo"

PROXIMO_NO_ANEL = {"veiculo_a_1": "veiculo_b_1", "veiculo_b_1": "veiculo_c_1"}
NOMES_FROTA     = ["veiculo_a_1", "veiculo_b_1", "veiculo_c_1"]

# Instância global do coletor — compartilhada entre os três agentes
mc = MetricsCollector()

# Dicionário compartilhado para coletar distâncias de todos os veículos
_distancias_frota: dict = {}


# ══════════════════════════════════════════════════════════════════════════════
# Agente Veículo
# ══════════════════════════════════════════════════════════════════════════════
class Veiculo(Agent):

    # ── Plano 1: recebe a ordem via ExternalChannel (broadcast) ───────────────
    @pl(gain, Belief("ordem_entrega", (Any, Any, Any)))
    def receber_ordem(self, src, dados):
        self._dados_ordem = dados
        transportadora, cep, custo = dados

        # G1 — marca o momento em que o Belief chegou ao primeiro agente
        mc.belief_injetado()

        self._distancia = round(random.uniform(0.1, 2.0), 2)
        _distancias_frota[self.my_name] = self._distancia

        self.print(
            f"Ordem recebida | CEP {cep} | "
            f"Distância sorteada: {self._distancia} km"
        )

        if self.my_name == "veiculo_a_1":
            time.sleep(0.2)

            # G2 — marca o início do protocolo de anel
            mc.anel_iniciado()

            self.print(f"Iniciando protocolo de anel ({self._distancia} km)")

            # Baseline — marca imediatamente antes do send do token (A->B)
            mc.baseline_envio()
            self.send(
                "veiculo_b_1",
                tell,
                Belief("token_anel", (self._distancia, self.my_name, dados)),
                CANAL_FROTA
            )

    # ── Plano 2: processa o token do anel ─────────────────────────────────────
    @pl(gain, Belief("token_anel", (Any, Any, Any)))
    def processar_token(self, src, token):
        # Baseline — marca a chegada do token; a guarda no coletor garante
        # que só a primeira passagem (A->B) é registrada
        mc.baseline_chegada()

        melhor_dist, melhor_nome, dados = token

        # G2 — conta cada passagem de token
        mc.token_passado()

        self.print(
            f"Token recebido | "
            f"Melhor até agora: {melhor_nome} ({melhor_dist} km) | "
            f"Minha distância: {self._distancia} km"
        )

        if self._distancia < melhor_dist:
            novo_dist = self._distancia
            novo_nome = self.my_name
        else:
            novo_dist = melhor_dist
            novo_nome = melhor_nome

        proximo = PROXIMO_NO_ANEL.get(self.my_name)

        if proximo:
            self.send(
                proximo,
                tell,
                Belief("token_anel", (novo_dist, novo_nome, dados)),
                CANAL_FROTA
            )
        else:
            self.print(f"Anel fechado! Vencedor: {novo_nome} ({novo_dist} km)")

            # G2/G3 — registra fechamento com todas as distâncias coletadas
            mc.anel_fechado(novo_nome, dict(_distancias_frota))

            for nome in NOMES_FROTA:
                self.send(
                    nome,
                    tell,
                    Belief("resultado_eleicao", (novo_nome, novo_dist, dados)),
                    CANAL_FROTA
                )

        self.rm(Belief("token_anel", token))

    # ── Plano 3: recebe o resultado da eleição ────────────────────────────────
    @pl(gain, Belief("resultado_eleicao", (Any, Any, Any)))
    def processar_resultado(self, src, resultado):
        eleito, dist_eleito, dados = resultado

        if eleito == self.my_name:
            self.print(
                f"Eleito para a entrega! "
                f"Distância: {self._distancia} km | "
                f"Tempo estimado: {round(self._distancia * FATOR_TEMPO, 1)}s"
            )
            self._executar_entrega(dados, dist_eleito)
        else:
            self.print(
                f"Recusado. Eleito: {eleito} ({dist_eleito} km) | "
                f"Minha distância: {self._distancia} km"
            )
            self._notificar_recusado(eleito, dist_eleito, dados)

        self.rm(Belief("resultado_eleicao", resultado))
        self.rm(Belief("ordem_entrega", dados))

    # ── Execução da entrega (veículo eleito) ──────────────────────────────────
    def _executar_entrega(self, dados, dist_eleito):
        transportadora, cep, custo = dados
        tempo = round(self._distancia * FATOR_TEMPO, 1)

        self.print(f"Saindo para entrega no CEP {cep}...")
        self._rastrear("saiu_para_entrega", cep)        # rastreamento -> JaCaMo
        time.sleep(tempo / 2)

        self.print(f"Em rota ({self._distancia} km)...")
        self._rastrear("em_rota", cep)                  # rastreamento -> JaCaMo
        time.sleep(tempo / 2)

        self.print(f"Entrega concluída no CEP {cep}!")
        self._rastrear("entrega_realizada", cep)        # rastreamento -> JaCaMo

        # G4 — marca a conclusão física da entrega
        mc.entrega_concluida()

        relatorio = {
            "vencedor":        self.my_name,
            "distancia_km":    self._distancia,
            "tempo_entrega_s": tempo,
            "transportadora":  transportadora,
            "cep":             cep,
            "custo":           custo,
            "concorrentes": [
                {"veiculo": n, "status": "recusado"}
                for n in NOMES_FROTA if n != self.my_name
            ],
        }

        canal = ExternalChannel.get_channel("veiculo_canal")
        canal.notify(
            URL_SPADE,
            FIPAMessage(
                performative = "inform",
                sender       = f"{self.my_name}@maspy",
                receiver     = AID_SPADE,
                content      = json.dumps(relatorio),
                ontology     = "logistica",
                priority     = 0,
            )
        )

        # G4 — marca o disparo do notify (fim do fluxo MASPY)
        mc.spade_confirmou()

        self.print("SPADE notificado com relatório completo.")

        # Exibe métricas desta execução e registra no histórico
        mc.imprimir_relatorio()
        mc.registrar_execucao()

        # Limpa distâncias para a próxima execução
        _distancias_frota.clear()

    # ── Notificação de recusa ao SPADE ────────────────────────────────────────
    def _notificar_recusado(self, eleito, dist_eleito, dados):
        transportadora, cep, custo = dados
        relatorio = {
            "veiculo":      self.my_name,
            "distancia_km": self._distancia,
            "status":       "recusado",
            "eleito":       eleito,
            "dist_eleito":  dist_eleito,
        }
        canal = ExternalChannel.get_channel("veiculo_canal")
        canal.notify(
            URL_SPADE,
            FIPAMessage(
                performative = "inform",
                sender       = f"{self.my_name}@maspy",
                receiver     = AID_SPADE,
                content      = json.dumps(relatorio),
                ontology     = "logistica",
                priority     = 1,
            )
        )

    # ── Rastreamento direto ao JaCaMo (Opção D) ──────────────────────────────
    def _rastrear(self, status, cep):
        """
        Envia um evento de rastreamento direto ao JaCaMo via jacamo-rest.

        Diferente do relatório ao SPADE (confirmação comercial, formato FIPA),
        este é o rastreamento logístico em tempo real — vem direto do veículo,
        como num app de delivery. Usa formato="jacamo-rest" para que o mesmo
        ExternalChannel traduza a FIPAMessage no literal Jason que o JaCaMo
        entende, sem alterar a lógica do agente.
        """
        canal = ExternalChannel.get_channel("veiculo_canal")
        canal.notify(
            URL_JACAMO,
            FIPAMessage(
                performative = "inform",
                sender       = f"{self.my_name}@maspy",
                receiver     = AID_JACAMO,
                content      = (
                    f'veiculo_status("{self.my_name}", "{status}", '
                    f'"{cep}", {self._distancia})'
                ),
                ontology     = "logistica",
                priority     = 1,
            ),
            formato = "jacamo-rest"
        )


# ── Transform FIPA ────────────────────────────────────────────────────────────
def transform_fipa_entrega(payload: dict) -> tuple:
    try:
        return (
            payload["transportadora"],
            payload["cep"],
            payload["custo"],
        )
    except KeyError as e:
        raise ValueError(f"Campo ausente no content FIPA: {e}")


# ── Sistema ───────────────────────────────────────────────────────────────────
def main():
    print("=" * 55)
    print("  MASPY — Frota Autônoma de Entrega v3.0")
    print("  Protocolo: FIPA-ACL + Eleição em anel")
    print("  Métricas : MetricsCollector (separado do canal)")
    print("=" * 55)

    canal = ExternalChannel.create("veiculo_canal", port=PORTA)
    canal.route(
        "/entrega",
        belief    = "ordem_entrega",
        transform = transform_fipa_entrega
    )

    # Registra mc.post_recebido() via callback do ExternalChannel
    # O canal chama on_post() antes de processar — sem modificar o canal
    _instalar_hook_post(canal)

    canal_interno = Channel(CANAL_FROTA)

    v1 = Veiculo("veiculo_a")
    v2 = Veiculo("veiculo_b")
    v3 = Veiculo("veiculo_c")

    Admin().connect_to([v1, v2, v3], [canal, canal_interno])

    print()
    print("  Frota pronta. Aguardando ordens do SPADE.")
    print(f"  Endpoint  : POST http://localhost:{PORTA}/entrega")
    print(f"  Status    : GET  http://localhost:{PORTA}/info")
    print(f"  Veículos  : veiculo_a_1, veiculo_b_1, veiculo_c_1")
    print(f"  Anel      : veiculo_a_1 -> veiculo_b_1 -> veiculo_c_1 -> fecha")
    print()
    print("=" * 55)

    try:
        Admin().start_system()
    except KeyboardInterrupt:
        pass
    finally:
        # Exibe relatório final e salva CSV ao encerrar
        print("\n[Encerrando experimento...]")
        mc.imprimir_relatorio_final()
        mc.salvar_csv("metricas_experimento.csv")


def _instalar_hook_post(canal):
    """
    Instala um hook no ExternalChannel para capturar o timestamp
    exato do POST — sem modificar o canal.

    Estratégia: substitui temporariamente o método _inject do canal
    por um wrapper que chama mc.post_recebido() antes de delegar.
    O _inject é chamado internamente pelo handle_post logo após
    validar a FIPAMessage — é o ponto mais próximo do POST.
    """
    _inject_original = canal._inject

    def _inject_com_metrica(data, target, priority=1):
        mc.post_recebido()
        return _inject_original(data, target, priority)

    canal._inject = _inject_com_metrica


if __name__ == "__main__":
    main()
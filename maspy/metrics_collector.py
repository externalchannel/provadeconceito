"""
metrics_collector.py
====================
Coleta de métricas do experimento de auto-organização em anel.

Princípio: completamente separado do ExternalChannel.
O canal não sabe que este módulo existe.

Métricas coletadas (4 grupos):
  G1 — ExternalChannel   : latência de injeção (POST → agent.add)
  G2 — Protocolo de anel : tempo de eleição, mensagens trocadas
  G3 — Qualidade         : taxa de acerto, distância vencedor vs média
  G4 — End-to-end        : latência total, variância entre execuções

Uso:
    from metrics_collector import MetricsCollector
    mc = MetricsCollector()

    mc.post_recebido()           # quando o POST chega
    mc.belief_injetado()         # quando agent.add() é chamado
    mc.anel_iniciado()           # quando veiculo_a_1 envia o token
    mc.token_passado()           # cada vez que um token é repassado
    mc.anel_fechado(vencedor, distancias)  # quando veiculo_c_1 fecha
    mc.entrega_concluida()       # quando o vencedor termina
    mc.spade_confirmou()         # quando SPADE recebe o inform final

    mc.registrar_execucao()      # salva os dados da execução atual
    mc.imprimir_relatorio()      # exibe relatório da execução atual
    mc.imprimir_relatorio_final()# exibe estatísticas de N execuções
    mc.salvar_csv("metricas.csv")# exporta todas as execuções para CSV
"""

import time
import csv
import statistics
from dataclasses import dataclass, field
from typing import Optional


# ══════════════════════════════════════════════════════════════════════════════
# Registro de uma execução
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class ExecucaoMetrica:
    """Dados coletados em uma única execução do experimento."""

    execucao_id:        int     = 0

    # G1 — ExternalChannel
    t_post_recebido:    float   = 0.0   # timestamp do POST chegando
    t_belief_injetado:  float   = 0.0   # timestamp do agent.add()

    # G2 — Protocolo de anel
    t_anel_iniciado:    float   = 0.0   # timestamp do primeiro token
    t_anel_fechado:     float   = 0.0   # timestamp do resultado_eleicao
    mensagens_anel:     int     = 0     # tokens trocados no anel

    # Baseline — entrega de UMA mensagem pelo canal nativo (token A->B)
    t_baseline_envio:   float   = 0.0   # antes do send do token em veiculo_a_1
    t_baseline_chegada: float   = 0.0   # 1a linha de processar_token em veiculo_b_1

    # G3 — Qualidade da eleição
    vencedor:           str     = ""
    dist_vencedor:      float   = 0.0
    distancias:         list    = field(default_factory=list)
    eleicao_correta:    bool    = True  # elegeu o de menor distância?

    # G4 — End-to-end
    t_entrega_concluida: float  = 0.0   # timestamp da entrega concluída
    t_spade_confirmou:   float  = 0.0   # timestamp do inform ao SPADE

    # ── Propriedades calculadas ───────────────────────────────────────────────

    @property
    def latencia_injecao_ms(self) -> float:
        """G1 — Tempo entre POST e agent.add() em milissegundos."""
        if self.t_belief_injetado and self.t_post_recebido:
            return round((self.t_belief_injetado - self.t_post_recebido) * 1000, 2)
        return 0.0

    @property
    def tempo_eleicao_s(self) -> float:
        """G2 — Tempo total do protocolo de anel em segundos."""
        if self.t_anel_fechado and self.t_anel_iniciado:
            return round(self.t_anel_fechado - self.t_anel_iniciado, 3)
        return 0.0

    @property
    def baseline_nativo_ms(self) -> float:
        """Baseline — entrega de uma mensagem (token A->B) pelo canal nativo,
        em milissegundos. Mesmo intervalo conceitual da latencia_injecao_ms:
        do envio ao início do plano que recebe."""
        if self.t_baseline_chegada and self.t_baseline_envio:
            return round((self.t_baseline_chegada - self.t_baseline_envio) * 1000, 2)
        return 0.0

    @property
    def dist_media(self) -> float:
        """G3 — Distância média de todos os veículos."""
        if self.distancias:
            return round(statistics.mean(self.distancias), 2)
        return 0.0

    @property
    def qualidade_eleicao(self) -> float:
        """G3 — Razão distância_vencedor / distância_média (< 1.0 = bom)."""
        if self.dist_media > 0:
            return round(self.dist_vencedor / self.dist_media, 3)
        return 0.0

    @property
    def latencia_end_to_end_s(self) -> float:
        """G4 — Tempo total do POST ao SPADE confirmar, em segundos."""
        if self.t_spade_confirmou and self.t_post_recebido:
            return round(self.t_spade_confirmou - self.t_post_recebido, 3)
        return 0.0

    @property
    def tempo_entrega_s(self) -> float:
        """G4 — Tempo da entrega em si (eleito saindo até concluir)."""
        if self.t_entrega_concluida and self.t_anel_fechado:
            return round(self.t_entrega_concluida - self.t_anel_fechado, 3)
        return 0.0


# ══════════════════════════════════════════════════════════════════════════════
# MetricsCollector
# ══════════════════════════════════════════════════════════════════════════════
class MetricsCollector:
    """
    Coleta e agrega métricas do experimento de auto-organização em anel.

    Completamente separado do ExternalChannel — o canal não é modificado.
    Os pontos de coleta são chamados explicitamente pelo veiculo_maspy.py.

    Suporta múltiplas execuções (N repetições do experimento).
    """

    def __init__(self):
        self._execucao_atual: ExecucaoMetrica = ExecucaoMetrica()
        self._historico:      list[ExecucaoMetrica] = []
        self._exec_id:        int = 0
        self._nova_execucao()

    def _nova_execucao(self):
        self._exec_id += 1
        self._execucao_atual = ExecucaoMetrica(execucao_id=self._exec_id)

    # ── Pontos de coleta ──────────────────────────────────────────────────────

    def post_recebido(self):
        """Chamar quando o POST do SPADE chegar (início do fluxo)."""
        self._execucao_atual.t_post_recebido = time.perf_counter()

    def belief_injetado(self):
        """Chamar quando o Belief for injetado no primeiro agente."""
        if self._execucao_atual.t_belief_injetado == 0.0:
            self._execucao_atual.t_belief_injetado = time.perf_counter()

    def anel_iniciado(self):
        """Chamar quando veiculo_a_1 envia o primeiro token."""
        self._execucao_atual.t_anel_iniciado = time.perf_counter()

    def token_passado(self):
        """Chamar cada vez que um token é repassado no anel."""
        self._execucao_atual.mensagens_anel += 1

    def baseline_envio(self):
        """Chamar imediatamente antes do send do token em veiculo_a_1."""
        self._execucao_atual.t_baseline_envio = time.perf_counter()

    def baseline_chegada(self):
        """Chamar na 1a linha de processar_token, só na 1a passagem (A->B)."""
        if self._execucao_atual.t_baseline_chegada == 0.0:
            self._execucao_atual.t_baseline_chegada = time.perf_counter()

    def anel_fechado(self, vencedor: str, distancias: dict):
        """
        Chamar quando veiculo_c_1 determina o vencedor.

        Parâmetros
        ----------
        vencedor : str
            Nome do veículo eleito.
        distancias : dict
            {nome_veiculo: distancia_km} de todos os participantes.
        """
        self._execucao_atual.t_anel_fechado  = time.perf_counter()
        self._execucao_atual.vencedor        = vencedor
        self._execucao_atual.distancias      = list(distancias.values())

        dist_v = distancias.get(vencedor, float("inf"))
        self._execucao_atual.dist_vencedor   = dist_v

        # Verifica se elegeu o de menor distância
        menor = min(distancias.values())
        self._execucao_atual.eleicao_correta = (dist_v == menor)

    def entrega_concluida(self):
        """Chamar quando o veículo eleito conclui a entrega."""
        self._execucao_atual.t_entrega_concluida = time.perf_counter()

    def spade_confirmou(self):
        """Chamar quando o notify() ao SPADE é disparado (fim do fluxo)."""
        self._execucao_atual.t_spade_confirmou = time.perf_counter()

    # ── Persistência de execuções ─────────────────────────────────────────────

    def registrar_execucao(self):
        """
        Salva a execução atual no histórico e prepara a próxima.
        Chamar ao final de cada execução completa.
        """
        self._historico.append(self._execucao_atual)
        self._nova_execucao()

    # ── Relatórios ────────────────────────────────────────────────────────────

    def imprimir_relatorio(self):
        """Exibe as métricas da execução atual no terminal."""
        e = self._execucao_atual
        sep = "─" * 52

        print(f"\n{sep}")
        print(f"  MÉTRICAS — Execução #{e.execucao_id}")
        print(sep)

        print(f"\n  G1 — ExternalChannel")
        print(f"    Latência de injeção   : {e.latencia_injecao_ms} ms")
        print(f"    Baseline canal nativo : {e.baseline_nativo_ms} ms")

        print(f"\n  G2 — Protocolo de anel")
        print(f"    Tempo de eleição      : {e.tempo_eleicao_s} s")
        print(f"    Mensagens no anel     : {e.mensagens_anel}")

        print(f"\n  G3 — Qualidade da eleição")
        print(f"    Vencedor              : {e.vencedor}")
        print(f"    Distância vencedor    : {e.dist_vencedor} km")
        print(f"    Distância média       : {e.dist_media} km")
        print(f"    Qualidade (v/média)   : {e.qualidade_eleicao}")
        print(f"    Eleição correta       : {'✓ SIM' if e.eleicao_correta else '✗ NÃO'}")
        print(f"    Distâncias individuais: {e.distancias}")

        print(f"\n  G4 — End-to-end")
        print(f"    Tempo de entrega      : {e.tempo_entrega_s} s")
        print(f"    Latência total        : {e.latencia_end_to_end_s} s")

        print(f"\n{sep}\n")

    def imprimir_relatorio_final(self):
        """
        Exibe estatísticas agregadas de todas as execuções registradas.
        Chamar após N execuções completas.
        """
        if not self._historico:
            print("[MetricsCollector] Nenhuma execução registrada ainda.")
            return

        n = len(self._historico)
        sep = "═" * 52

        def avg(valores): return round(statistics.mean(valores), 3)
        def std(valores): return round(statistics.stdev(valores), 3) if len(valores) > 1 else 0.0
        def mn(valores):  return round(min(valores), 3)
        def mx(valores):  return round(max(valores), 3)

        injecoes   = [e.latencia_injecao_ms  for e in self._historico]
        baselines  = [e.baseline_nativo_ms   for e in self._historico if e.baseline_nativo_ms > 0]
        tempos_el  = [e.tempo_eleicao_s      for e in self._historico]
        msgs       = [e.mensagens_anel       for e in self._historico]
        qualidades = [e.qualidade_eleicao    for e in self._historico]
        e2e        = [e.latencia_end_to_end_s for e in self._historico]
        acertos    = sum(1 for e in self._historico if e.eleicao_correta)

        print(f"\n{sep}")
        print(f"  RELATÓRIO FINAL — {n} execuções")
        print(sep)

        print(f"\n  G1 — ExternalChannel (latência de injeção)")
        print(f"    Média   : {avg(injecoes)} ms")
        print(f"    Desvio  : {std(injecoes)} ms")
        print(f"    Mín/Máx : {mn(injecoes)} / {mx(injecoes)} ms")

        print(f"\n  Baseline — canal nativo (token A->B)")
        if baselines:
            print(f"    Média   : {avg(baselines)} ms")
            print(f"    Desvio  : {std(baselines)} ms")
            print(f"    Mín/Máx : {mn(baselines)} / {mx(baselines)} ms")
            print(f"    Amostras: {len(baselines)}/{n}")
        else:
            print(f"    (sem amostras — verifique as marcas baseline no veiculo_maspy)")

        print(f"\n  G2 — Protocolo de anel")
        print(f"    Tempo eleição — Média   : {avg(tempos_el)} s")
        print(f"    Tempo eleição — Desvio  : {std(tempos_el)} s")
        print(f"    Tempo eleição — Mín/Máx : {mn(tempos_el)} / {mx(tempos_el)} s")
        print(f"    Mensagens/execução      : {avg(msgs)} (fixo: 2 tokens + 3 resultados = 5)")

        print(f"\n  G3 — Qualidade da eleição")
        print(f"    Taxa de acerto          : {acertos}/{n} ({round(acertos/n*100, 1)}%)")
        print(f"    Qualidade média (v/med) : {avg(qualidades)}")
        print(f"    Qualidade — Desvio      : {std(qualidades)}")

        print(f"\n  G4 — End-to-end")
        print(f"    Latência total — Média  : {avg(e2e)} s")
        print(f"    Latência total — Desvio : {std(e2e)} s")
        print(f"    Latência total — Mín/Máx: {mn(e2e)} / {mx(e2e)} s")

        print(f"\n{sep}\n")

    def salvar_csv(self, caminho: str = "metricas_experimento.csv"):
        """
        Exporta todas as execuções para CSV.
        Cada linha é uma execução. Pronto para importar no Excel ou Python.
        """
        if not self._historico:
            print("[MetricsCollector] Nenhuma execução para exportar.")
            return

        campos = [
            "execucao_id",
            "latencia_injecao_ms",
            "baseline_nativo_ms",
            "tempo_eleicao_s",
            "mensagens_anel",
            "vencedor",
            "dist_vencedor",
            "dist_media",
            "qualidade_eleicao",
            "eleicao_correta",
            "tempo_entrega_s",
            "latencia_end_to_end_s",
        ]

        with open(caminho, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=campos)
            writer.writeheader()
            for e in self._historico:
                writer.writerow({
                    "execucao_id":          e.execucao_id,
                    "latencia_injecao_ms":  e.latencia_injecao_ms,
                    "baseline_nativo_ms":   e.baseline_nativo_ms,
                    "tempo_eleicao_s":      e.tempo_eleicao_s,
                    "mensagens_anel":       e.mensagens_anel,
                    "vencedor":             e.vencedor,
                    "dist_vencedor":        e.dist_vencedor,
                    "dist_media":           e.dist_media,
                    "qualidade_eleicao":    e.qualidade_eleicao,
                    "eleicao_correta":      e.eleicao_correta,
                    "tempo_entrega_s":      e.tempo_entrega_s,
                    "latencia_end_to_end_s": e.latencia_end_to_end_s,
                })

        print(f"[MetricsCollector] CSV salvo em: {caminho}")
        print(f"[MetricsCollector] {len(self._historico)} execuções exportadas.")

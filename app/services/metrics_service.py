"""Métricas LOCAIS de uso. Só METADADOS (quando, qual tipo, modelo, tokens informados pelo provedor, duração, resultado):
nunca o conteúdo de conversas, voz, e-mails ou credenciais. Uma solicitação = um evento (a chave é o id da submissão), então
sinais repetidos não duplicam e uma repetição bem-sucedida substitui a falha anterior da MESMA solicitação.
Custo: só quando o usuário informa o preço do modelo (settings.json, metrics.prices); sem isso é "indisponível"."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta, timezone

from app.database.database import Database, DatabaseError

log = logging.getLogger("jarvis.metrics")
PERIODS = {"today": "Hoje", "7d": "Últimos 7 dias", "30d": "Últimos 30 dias", "all": "Tudo"}


@dataclass
class MetricsSummary:
    period: str
    interactions: int = 0
    ai_requests: int = 0
    ai_attempts: int = 0  # inclui repetições manuais da mesma solicitação
    local_commands: int = 0
    errors: int = 0
    input_tokens: int = 0  # MEDIDOS (informados pelo provedor)
    output_tokens: int = 0
    requests_with_tokens: int = 0
    requests_without_tokens: int = 0  # chamadas bem-sucedidas sem número de tokens: não entram na soma
    avg_duration_s: float | None = None
    by_model: dict[str, dict] = field(default_factory=dict)
    by_day: dict[str, int] = field(default_factory=dict)
    by_category: dict[str, int] = field(default_factory=dict)
    cost_usd: float | None = None  # ESTIMADO (preço informado pelo usuário); None = indisponível
    cost_note: str = "Custo indisponível: informe o preço do modelo em settings.json (metrics.prices) para ver uma estimativa."


class MetricsService:
    def __init__(self, db: Database | None, prices=None, tz=None) -> None:
        self.db, self._prices, self._tz = db, prices, tz or datetime.now().astimezone().tzinfo or timezone.utc

    @property
    def available(self) -> bool:
        return self.db is not None and self.db.is_open

    # --- registro (melhor esforço: nunca atrapalha o atendimento) ---
    def record(self, *, submission_id: str | None, kind: str, ok: bool, category: str | None = None, source: str | None = None, model: str | None = None,
               input_tokens: int | None = None, output_tokens: int | None = None, duration_s: float | None = None, error_kind: str | None = None,
               attempts: int = 1, ts: datetime | None = None) -> bool:
        if self.db is None or kind not in ("ai", "local"):
            return False
        ts = (ts or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        sql = ("INSERT INTO usage_events (ts, submission_id, kind, category, source, model, input_tokens, output_tokens, duration_s, ok, error_kind, attempts) "
               "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
        params = (ts, submission_id, kind, category, source, model, input_tokens, output_tokens, duration_s, 1 if ok else 0, (error_kind or None), max(1, attempts))
        if submission_id:  # a MESMA solicitação atualiza o seu evento (resultado final), nunca cria um segundo
            sql += (" ON CONFLICT(submission_id) DO UPDATE SET ts = excluded.ts, kind = excluded.kind, category = excluded.category, model = excluded.model, "
                    "input_tokens = excluded.input_tokens, output_tokens = excluded.output_tokens, duration_s = excluded.duration_s, ok = excluded.ok, "
                    "error_kind = excluded.error_kind, attempts = excluded.attempts")
        try:
            with self.db.transaction() as c:
                c.execute(sql, params)
            return True
        except DatabaseError as exc:
            log.warning("[METRICS] evento não registrado: %s", exc)
            return False

    # --- consulta ---
    def _since(self, period: str, now: datetime | None = None) -> str | None:
        now = (now or datetime.now(self._tz)).astimezone(self._tz)
        days = {"today": 0, "7d": 6, "30d": 29}.get(period)
        if days is None:
            return None
        start = datetime.combine(now.date() - timedelta(days=days), dtime.min, tzinfo=self._tz)
        return start.astimezone(timezone.utc).isoformat()

    def summary(self, period: str = "7d", now: datetime | None = None) -> MetricsSummary:
        s = MetricsSummary(period if period in PERIODS else "7d")
        since = self._since(s.period, now)
        if self.db is None:
            return s
        where, params = ("WHERE ts >= ?", (since,)) if since else ("", ())
        with self.db.connect() as c:
            rows = c.execute(f"SELECT ts, kind, category, model, input_tokens, output_tokens, duration_s, ok, attempts FROM usage_events {where}", params).fetchall()
        durations: list[float] = []
        for r in rows:
            s.interactions += 1
            day = datetime.fromisoformat(r["ts"]).astimezone(self._tz).date().isoformat()
            s.by_day[day] = s.by_day.get(day, 0) + 1
            if not r["ok"]:
                s.errors += 1
            if r["kind"] == "local":
                s.local_commands += 1
                s.by_category[r["category"] or "outros"] = s.by_category.get(r["category"] or "outros", 0) + 1
                continue
            s.ai_requests += 1
            s.ai_attempts += r["attempts"]
            m = s.by_model.setdefault(r["model"] or "modelo não informado", {"requests": 0, "input": 0, "output": 0, "measured": 0})
            m["requests"] += 1
            if r["ok"] and r["input_tokens"] is not None and r["output_tokens"] is not None:
                s.input_tokens += r["input_tokens"]
                s.output_tokens += r["output_tokens"]
                s.requests_with_tokens += 1
                m["input"] += r["input_tokens"]
                m["output"] += r["output_tokens"]
                m["measured"] += 1
            elif r["ok"]:
                s.requests_without_tokens += 1
            if r["duration_s"] is not None:
                durations.append(r["duration_s"])
        s.avg_duration_s = sum(durations) / len(durations) if durations else None
        s.by_day = dict(sorted(s.by_day.items()))
        self._estimate_cost(s)
        return s

    def _estimate_cost(self, s: MetricsSummary) -> None:
        prices = self._prices() if callable(self._prices) else (self._prices or {})
        total, missing = 0.0, []
        for model, m in s.by_model.items():
            p = prices.get(model) if isinstance(prices, dict) else None
            if m["measured"] == 0:
                continue
            if not (isinstance(p, dict) and isinstance(p.get("input"), (int, float)) and isinstance(p.get("output"), (int, float))):
                missing.append(model)
                continue
            total += m["input"] * p["input"] / 1_000_000 + m["output"] * p["output"] / 1_000_000
        if s.requests_with_tokens and not missing and prices:
            s.cost_usd = round(total, 4)
            s.cost_note = "Custo ESTIMADO com os preços que você informou (US$ por milhão de tokens) e os tokens medidos."
        elif missing:
            s.cost_note = "Custo indisponível: falta o preço de " + ", ".join(missing) + " em settings.json (metrics.prices)."

    # --- limpeza (só os eventos de uso; conversas, memórias, preferências e integrações não são tocadas) ---
    def clear(self) -> int:
        if self.db is None:
            return 0
        with self.db.transaction() as c:
            return c.execute("DELETE FROM usage_events").rowcount


def format_summary(s: MetricsSummary) -> str:
    """Texto do painel. Diferencia o que foi MEDIDO (informado pelo provedor) do que é ESTIMADO (só o custo, com preços do usuário)."""
    n = lambda v: f"{v:,}".replace(",", ".")  # noqa: E731
    if s.interactions == 0:
        return f"Período: {PERIODS[s.period]}\n\nAinda não há dados de uso neste período. Nada é estimado nem inventado."
    lines = [f"Período: {PERIODS[s.period]}", "",
             f"Interações: {n(s.interactions)}  (IA: {n(s.ai_requests)} · comandos locais: {n(s.local_commands)} · erros: {n(s.errors)})"]
    if s.ai_requests:
        lines.append(f"Chamadas à IA: {n(s.ai_requests)}" + (f" (com repetições: {n(s.ai_attempts)} tentativas)" if s.ai_attempts != s.ai_requests else ""))
        lines.append(f"Tokens MEDIDOS (informados pelo provedor): entrada {n(s.input_tokens)} · saída {n(s.output_tokens)}  — em {n(s.requests_with_tokens)} chamada(s)"
                     + (f"; {n(s.requests_without_tokens)} chamada(s) sem esse dado não entram na soma" if s.requests_without_tokens else ""))
        lines.append("Duração média das chamadas: " + (f"{s.avg_duration_s:.1f} s".replace(".", ",") if s.avg_duration_s is not None else "não disponível"))
        for model, m in s.by_model.items():
            lines.append(f"  • {model}: {n(m['requests'])} chamada(s)" + (f" · entrada {n(m['input'])} · saída {n(m['output'])}" if m["measured"] else " · sem tokens medidos"))
    if s.by_category:
        lines.append("Comandos locais: " + " · ".join(f"{k} {v}" for k, v in sorted(s.by_category.items(), key=lambda kv: -kv[1])))
    lines += ["", "Por dia: " + (" · ".join(f"{d[8:]}/{d[5:7]}: {c}" for d, c in s.by_day.items()) or "—"), ""]
    lines.append((f"Custo estimado: US$ {s.cost_usd:.4f}\n" if s.cost_usd is not None else "Custo: indisponível\n") + s.cost_note)
    return "\n".join(lines)

"""Rotinas personalizadas: cadastro, voz e execução. Só combina capacidades que JÁ existem e as executa pelos mesmos caminhos
seguros: ActionService (abrir/fechar, com a confirmação da Etapa 7) e IntegrationManager (agenda, e-mails, notícias, clima, resumo).
Não há script, shell nem código livre; criar uma rotina não autoriza nada; uma etapa que exige confirmação PAUSA a rotina."""
from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.core.action_intents import ActionParse
from app.core.actions import ActionKind, ActionStatus, PendingConfirmation
from app.core.integration_intents import IntegrationCommand, IntegrationKind
from app.core.intents import _M_PREFIX
from app.core.routines import (STEP_TYPES, Routine, RoutineStep, describe_step, name_key, new_id, steps_from_json, steps_to_json, validate_name,
                               validate_steps, validate_trigger)
from app.core.textutil import fold, norm_alias
from app.database.database import Database, DatabaseError, IntegrityViolation
from app.integrations import answers

log = logging.getLogger("jarvis.routines")
_UNCONFIGURED = (answers.CAL_UNCONFIGURED, answers.EMAIL_UNCONFIGURED, answers.WEATHER_UNCONFIGURED)
_RUN = re.compile(r"^(?:(?:execute|executar|rode|rodar|inicie|iniciar|faca|fazer|dispare|disparar)\s+(?:a\s+|minha\s+)*rotina|(?:minha\s+)?rotina)\s+(?P<n>.+?)[\s.!?]*$")


@dataclass(frozen=True)
class StepResult:
    index: int
    type: str
    status: str  # ok | failed | skipped | awaiting
    message: str = ""


@dataclass(frozen=True)
class RoutineResult:
    status: str  # ok | partial | failed | awaiting_confirmation | refused
    text: str
    name: str = ""
    steps: tuple[StepResult, ...] = ()
    pending: PendingConfirmation | None = None
    cleared: tuple[tuple[str, str], ...] = ()
    private: bool = False  # alguma etapa trouxe agenda/e-mail: o texto não volta à IA como contexto


@dataclass(frozen=True)
class RoutineMatch:
    routine: Routine | None
    message: str = ""  # quando não achou ou não pode executar, o que dizer ao usuário


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RoutineRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def _routine(self, r) -> Routine:
        steps, ok = steps_from_json(r["steps"])
        return Routine(r["id"], r["name"], steps, bool(r["enabled"]), r["trigger_phrase"] or "", ok)

    def list(self) -> list[Routine]:
        with self.db.connect() as c:
            return [self._routine(r) for r in c.execute("SELECT * FROM routines ORDER BY name_key")]

    def get(self, rid: str) -> Routine | None:
        with self.db.connect() as c:
            r = c.execute("SELECT * FROM routines WHERE id = ?", (rid,)).fetchone()
        return self._routine(r) if r else None

    def save(self, routine: Routine, creating: bool) -> None:
        now = _now()
        with self.db.transaction() as c:
            if creating:
                c.execute("INSERT INTO routines (id, name, name_key, enabled, trigger_phrase, steps, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                          (routine.id, routine.name, name_key(routine.name), 1 if routine.enabled else 0, routine.trigger or None, steps_to_json(routine.steps), now, now))
            elif c.execute("UPDATE routines SET name = ?, name_key = ?, trigger_phrase = ?, steps = ?, updated_at = ? WHERE id = ?",
                           (routine.name, name_key(routine.name), routine.trigger or None, steps_to_json(routine.steps), now, routine.id)).rowcount == 0:
                raise DatabaseError("Essa rotina não existe mais.")

    def set_enabled(self, rid: str, enabled: bool) -> bool:
        with self.db.transaction() as c:
            return c.execute("UPDATE routines SET enabled = ?, updated_at = ? WHERE id = ?", (1 if enabled else 0, _now(), rid)).rowcount > 0

    def delete(self, rid: str) -> bool:
        with self.db.transaction() as c:
            return c.execute("DELETE FROM routines WHERE id = ?", (rid,)).rowcount > 0

    def start_run(self, rid: str, name: str, source: str) -> str:
        run_id = new_id()
        with self.db.transaction() as c:
            c.execute("INSERT INTO routine_runs (id, routine_id, routine_name, started_at, status, source) VALUES (?, ?, ?, ?, 'running', ?)", (run_id, rid, name, _now(), source))
        return run_id

    def finish_run(self, run_id: str, status: str, results: list[dict]) -> None:
        with self.db.transaction() as c:  # só ÍNDICE, tipo e situação de cada etapa: nenhum texto de agenda, e-mail ou notícia
            c.execute("UPDATE routine_runs SET finished_at = ?, status = ?, results = ? WHERE id = ?", (_now(), status, json.dumps(results), run_id))

    def last_runs(self, rid: str, n: int = 5) -> list[dict]:
        with self.db.connect() as c:
            rows = c.execute("SELECT started_at, finished_at, status, source, results FROM routine_runs WHERE routine_id = ? ORDER BY started_at DESC LIMIT ?", (rid, n)).fetchall()
        return [{"started_at": r["started_at"], "finished_at": r["finished_at"], "status": r["status"], "source": r["source"], "results": json.loads(r["results"] or "[]")} for r in rows]

    def mark_interrupted(self) -> int:
        with self.db.transaction() as c:
            return c.execute("UPDATE routine_runs SET status = 'interrupted', finished_at = ? WHERE status = 'running'", (_now(),)).rowcount


class RoutineService:
    def __init__(self, repo: RoutineRepository, actions=None, integrations=None) -> None:
        self.repo, self.actions, self.integrations = repo, actions, integrations
        self._running: set[str] = set()
        self._lock = threading.Lock()
        try:
            n = repo.mark_interrupted()  # uma execução que ficou "em andamento" de uma sessão anterior foi interrompida
            if n:
                log.warning("[ROUTINES] %d execução(ões) anterior(es) foram interrompidas", n)
        except DatabaseError as exc:
            log.warning("[ROUTINES] não consegui revisar execuções antigas: %s", exc)

    # --- cadastro ---
    def list(self) -> list[Routine]:
        return self.repo.list()

    def get(self, rid: str) -> Routine | None:
        return self.repo.get(rid)

    def save(self, name: str, trigger: str, raw_steps, rid: str | None = None, enabled: bool = True) -> tuple[Routine | None, list[str]]:
        name, e1 = validate_name(name)
        trig, e2 = validate_trigger(trigger)
        steps, e3 = validate_steps(raw_steps)
        errors = [e for e in (e1, e2) if e] + e3
        if errors:
            return None, errors
        for other in self.repo.list():
            if other.id != rid and name_key(other.name) == name_key(name):
                return None, ["Já existe uma rotina com esse nome."]
            if trig and other.id != rid and other.trigger and fold(other.trigger).strip(" .!?") == fold(trig).strip(" .!?"):
                return None, ["Outra rotina já usa essa frase de voz."]
        current = self.repo.get(rid) if rid else None
        if rid and current is None:
            return None, ["Essa rotina não existe mais."]
        routine = Routine(rid or new_id(), name, steps, current.enabled if current else enabled, trig)
        try:
            self.repo.save(routine, creating=rid is None)
        except IntegrityViolation:
            return None, ["Já existe uma rotina com esse nome."]
        return routine, []

    def set_enabled(self, rid: str, enabled: bool) -> bool:
        return self.repo.set_enabled(rid, enabled)

    def delete(self, rid: str) -> bool:
        return self.repo.delete(rid)

    # --- voz e texto ---
    def match(self, text: str) -> RoutineMatch | None:
        """None = não é um pedido de rotina. Só a frase do USUÁRIO é interpretada; achar uma rotina não a autoriza a nada."""
        folded = fold(text or "")
        rest = folded[_M_PREFIX.match(folded).end():].strip(" \t\n.!?,")
        if not rest:
            return None
        try:
            routines = self.repo.list()
        except DatabaseError:
            return None
        m = _RUN.match(rest)
        wanted = norm_alias(m.group("n")) if m else None
        found = [r for r in routines if (wanted is not None and name_key(r.name) == wanted) or (r.trigger and fold(r.trigger).strip(" .!?,") == rest)]
        if not found:
            if m is None:
                return None
            names = ", ".join(r.name for r in routines[:6])
            return RoutineMatch(None, f"Não encontrei essa rotina." + (f" Rotinas: {names}." if names else " Você ainda não criou nenhuma (Configurações → Rotinas)."))
        r = found[0]
        if not r.enabled:
            return RoutineMatch(None, f"A rotina “{r.name}” está desativada. Ative em Configurações → Rotinas.")
        return RoutineMatch(r)

    # --- execução ---
    def run(self, rid: str, source: str = "manual") -> RoutineResult:
        routine = self.repo.get(rid)
        if routine is None:
            return RoutineResult("refused", "Essa rotina não existe mais.")
        if not routine.enabled:
            return RoutineResult("refused", f"A rotina “{routine.name}” está desativada.", routine.name)
        if not routine.valid or not routine.steps:
            return RoutineResult("refused", f"Os dados da rotina “{routine.name}” estão inválidos; edite e salve de novo.", routine.name)
        with self._lock:
            if rid in self._running:
                return RoutineResult("refused", f"A rotina “{routine.name}” já está em execução.", routine.name)  # sinal duplicado: não roda duas vezes
            self._running.add(rid)
        run_id = None
        try:
            try:
                run_id = self.repo.start_run(rid, routine.name, source)
            except DatabaseError as exc:
                log.warning("[ROUTINES] execução sem registro: %s", exc)
            result = self._execute(routine, source)
            if run_id:
                self._finish(run_id, result)
            return result
        finally:
            with self._lock:
                self._running.discard(rid)

    def _finish(self, run_id: str, result: RoutineResult) -> None:
        try:
            self.repo.finish_run(run_id, result.status, [{"i": s.index, "type": s.type, "status": s.status} for s in result.steps])
        except DatabaseError as exc:
            log.warning("[ROUTINES] resultado não registrado: %s", exc)

    def _execute(self, routine: Routine, source: str) -> RoutineResult:
        results: list[StepResult] = []
        pending, cleared, private, stopped = None, (), False, None
        for i, step in enumerate(routine.steps, 1):
            if stopped:
                results.append(StepResult(i, step.type, "skipped"))
                continue
            try:
                status, message, outcome, is_private = self._step(step, source)
            except Exception:  # noqa: BLE001 - uma etapa com defeito não derruba o aplicativo
                log.exception("[ROUTINES] falha inesperada na etapa %d (%s)", i, step.type)
                status, message, outcome, is_private = "failed", "Falha inesperada nesta etapa.", None, False
            private = private or is_private
            results.append(StepResult(i, step.type, status, message))
            if outcome is not None:
                pending, cleared = outcome.pending, cleared + tuple(outcome.cleared)
            if status == "awaiting":
                stopped = "awaiting"  # a confirmação é pelo mecanismo da Etapa 7; as etapas seguintes NÃO rodam sozinhas depois dela
            elif status == "failed" and step.essential:
                stopped = "failed"
        failed_any = any(r.status == "failed" for r in results)
        status = "awaiting_confirmation" if stopped == "awaiting" else "failed" if stopped == "failed" else "partial" if failed_any else "ok"
        return RoutineResult(status, self._text(routine, results, stopped), routine.name, tuple(results), pending, cleared, private)

    def _text(self, routine: Routine, results: list[StepResult], stopped: str | None) -> str:
        lines = []
        for r in results:
            label = STEP_TYPES[r.type].label
            if r.status == "skipped":
                lines.append(f"{r.index}) {label}: não executada.")
            else:
                msg = " ".join(r.message.split())
                lines.append(f"{r.index}) {label}: " + (msg if len(msg) <= 300 else msg[:299].rstrip() + "…"))
        head = {"ok": f"Rotina “{routine.name}” concluída.", "partial": f"Rotina “{routine.name}” concluída com falhas em etapas opcionais.",
                "failed": f"Rotina “{routine.name}” interrompida: uma etapa essencial falhou.",
                "awaiting": f"Rotina “{routine.name}” pausada: aguardando sua confirmação. As etapas seguintes não foram executadas."}[stopped or ("partial" if any(r.status == "failed" for r in results) else "ok")]
        return head + "\n" + "\n".join(lines)

    def _step(self, step: RoutineStep, source: str):
        t, v = step.type, step.value
        if t in ("agenda", "email", "news", "weather", "morning"):
            if self.integrations is None:
                return "failed", "As integrações não estão disponíveis.", None, False
            kind = {"agenda": IntegrationKind.AGENDA, "email": IntegrationKind.EMAIL, "news": IntegrationKind.NEWS, "weather": IntegrationKind.WEATHER, "morning": IntegrationKind.MORNING}[t]
            cmd = IntegrationCommand(kind, period=v if t == "agenda" else "", mode=v if t == "email" else "", topic=fold(v) if t == "news" else "", when=v if t == "weather" else "")
            reply = self.integrations.answer(cmd)
            bad = (not reply.ok) or reply.text in _UNCONFIGURED or reply.text.startswith("Não consegui acessar") or reply.text.startswith("Não acompanho")
            return ("failed" if bad else "ok"), reply.text, None, True
        if self.actions is None or not self.actions.enabled:
            return "failed", "O controle do computador está desativado.", None, False
        parse = {"open_app": ActionParse("open", "app", norm_alias(v), v), "open_folder": ActionParse("open", "pasta", norm_alias(v), v),
                 "open_project": ActionParse("open", "projeto", norm_alias(v), v),
                 "open_site": ActionParse("open", "site", norm_alias(v) if not re.match(r"https?://", v) else "", v),
                 "spotify_search": ActionParse("spotify", raw=v), "close_app": ActionParse("close", "", norm_alias(v), v)}[t]
        outcome = self.actions.handle(parse, source)
        s = outcome.result.status
        if s is ActionStatus.CONFIRMATION_REQUIRED:
            return "awaiting", outcome.result.message, outcome, False
        return ("ok" if s in (ActionStatus.SUCCESS, ActionStatus.DUPLICATE) else "failed"), outcome.result.message, outcome, False

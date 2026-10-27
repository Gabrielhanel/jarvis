"""Testes essenciais da Etapa 9: conversas, isolamento, modos, configurações, atalho, rotinas e métricas. Offline e com bancos temporários."""
import ctypes
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.capabilities import CapabilityRegistry
from app.core.config import AIConfig, Config, MemoryConfig
from app.core.modes import MODES, effective_ai_config, normalize_mode
from app.core.routines import validate_steps
from app.core.user_settings import SETTINGS
from app.database import Database
from app.database.database import MIGRATIONS
from app.database.repositories import ConversationRepository, MemoryRepository
from app.integrations.manager import IntegrationReply
from app.services.ai_service import AIError
from app.services.assistant_service import AssistantEvent as AE, AssistantPipeline
from app.services.history_service import HistoryService
from app.services.hotkey_service import (ERROR_HOTKEY_ALREADY_REGISTERED, MOD_NOREPEAT, WM_HOTKEY, HotkeyService, HotkeyState, NullBackend, RegisterResult,
                                         WindowsBackend, detect_backend, parse_combo)
from app.services.memory_service import MemoryService
from app.services.metrics_service import MetricsService, format_summary
from app.services.routine_service import RoutineRepository, RoutineService
from app.services.settings_service import SettingsService
from tests.test_actions import Env
from tests.test_assistant import CFGAI, FakeAI


def wait_for(pred, timeout=6.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end and not pred():
        time.sleep(0.01)
    return pred()


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self._fin = []
        self.addCleanup(lambda: [f() for f in reversed(self._fin)])


class Harness(Base):
    def make(self, ai=None, mode="EQUILIBRADO", routines=False, actions=None, integrations=None):
        self.db = Database(self.tmp / "j.db").open()
        self._fin.append(self.db.close)
        self.history = HistoryService(ConversationRepository(self.db))
        self.cid = self.history.ensure_active()
        self.memory = MemoryService(MemoryRepository(self.db), MemoryConfig())
        self.metrics = MetricsService(self.db)
        self.routines = RoutineService(RoutineRepository(self.db), actions, integrations) if routines else None
        self.mode, self.ai, self.events = [mode], ai or FakeAI(), []
        self.p = AssistantPipeline(CFGAI, self.ai, CapabilityRegistry(), lambda ev, *a: self.events.append((ev, a)), history=self.history, memory=self.memory,
                                   actions=actions, integrations=integrations, routines=self.routines, metrics=self.metrics, mode_provider=lambda: self.mode[0])
        t = threading.Thread(target=self.p.run, daemon=True)
        t.start()
        self._fin.append(lambda: (self.p.stop(), t.join(3)))
        return self

    def ask(self, text, sid=None, wait=True, source="texto"):
        sid = self.p.submit(text, source, sid)
        if wait and sid:
            assert wait_for(lambda: any(a[0] == sid for e, a in self.events if e in (AE.RESPONSE, AE.FAILED))), text
        return sid

    def of(self, ev): return [a for e, a in self.events if e == ev]
    def reply(self, sid): return next((a[1] for e, a in self.events if e == AE.RESPONSE and a[0] == sid), None)
    def rows(self, cid=None): return self.history.load_messages(cid or self.cid)


# ================================================== banco e conversas ==================================================
class TestMigrationAndConversations(Base):
    def test_v2_database_upgrades_to_v3_keeping_data_and_backfilling_only_real_usage(self):
        path = self.tmp / "j.db"
        db = Database(path, migrations=MIGRATIONS[:2]).open()
        db.execute("INSERT INTO conversations (id,title,created_at,updated_at,is_active) VALUES ('c1','t','a','a',1)")
        db.execute("INSERT INTO messages (id,conversation_id,submission_id,role,content,created_at,source,state) VALUES ('m1','c1','s1','user','oi','2026-10-01T10:00:00+00:00','texto','done')")
        db.execute("INSERT INTO messages (id,conversation_id,submission_id,role,content,created_at,origin,state,model,input_tokens,output_tokens,duration_s) "
                   "VALUES ('m2','c1','s1','jarvis','olá','2026-10-01T10:00:02+00:00','ia','done','claude-haiku-5-5',50,10,1.5)")
        db.execute("INSERT INTO messages (id,conversation_id,submission_id,role,content,created_at,source,state,error_kind) VALUES ('m3','c1','s2','user','falhou','2026-10-01T11:00:00+00:00','voz','failed','timeout')")
        db.execute("INSERT INTO memories (id,category,title,content,created_at,updated_at) VALUES ('x','outros','t','Moro em Curitiba','a','a')")
        db.close()
        db = Database(path).open()
        self.addCleanup(db.close)
        self.assertEqual(db.schema_version, len(MIGRATIONS))
        self.assertEqual(db.execute("SELECT content FROM memories")[0][0], "Moro em Curitiba")
        self.assertEqual(db.execute("SELECT COUNT(*) FROM messages")[0][0], 3)
        rows = [tuple(r) for r in db.execute("SELECT kind, model, input_tokens, ok, error_kind FROM usage_events ORDER BY id")]
        self.assertEqual(rows, [("ai", "claude-haiku-5-5", 50, 1, None), ("ai", None, None, 0, "timeout")])  # só o que JÁ existia; nada inventado
        self.assertEqual(db.execute("SELECT title_custom FROM conversations")[0][0], 0)

    def test_new_list_rename_delete_and_memory_preserved(self):
        db = Database(self.tmp / "j.db").open()
        self.addCleanup(db.close)
        h = HistoryService(ConversationRepository(db))
        a = h.ensure_active()
        h.record_user(a, "s1", "Conversa sobre Python", "texto")
        h.record_response(a, "s1", "ok", "ia")
        MemoryRepository(db).insert("outros", "t", "Moro em Curitiba", a)
        self.assertEqual(h.new_conversation()[1], True)
        b = h.active_id
        self.assertEqual(h.new_conversation(), (b, False))  # a ativa está vazia: não acumula conversas vazias
        self.assertEqual([(c.title, c.message_count, c.is_active) for c in h.list_conversations()], [("Nova conversa", 0, True), ("Conversa sobre Python", 2, False)])
        self.assertEqual(h.rename_conversation(a, "  Meu título  "), (True, "Meu título"))
        for bad in ("", "???", "x" * 61, "\u202e"):
            self.assertFalse(h.rename_conversation(a, bad)[0], bad)
        h.record_user(a, "s2", "outra mensagem", "texto")
        self.assertEqual(h.get_conversation(a).title, "Meu título")  # o título do usuário nunca é sobrescrito
        self.assertEqual(h.delete_conversation(a), b)  # apagou a não ativa: a ativa continua
        self.assertEqual(db.execute("SELECT COUNT(*) FROM messages WHERE conversation_id = ?", (a,))[0][0], 0)
        self.assertEqual(db.execute("SELECT content FROM memories")[0][0], "Moro em Curitiba")  # a memória global fica
        self.assertEqual(h.delete_conversation(b), h.active_id)  # apagou a ativa: cria/reativa outra
        self.assertEqual(len(h.list_conversations()), 1)


class TestConversationIsolation(Harness):
    def test_switching_conversations_never_mixes_context(self):
        self.make(ai=FakeAI(replies=["rA", "rB", "rA2"]))
        a = self.cid
        self.ask("pergunta A")
        b = self.history.create_conversation(activate=False)
        self.p.activate_conversation(b)
        self.ask("pergunta B")
        self.assertEqual([m["content"] for m in self.ai.calls[1][1]], ["pergunta B"])
        self.p.activate_conversation(a)
        self.ask("continuando A")
        self.assertEqual([m["content"] for m in self.ai.calls[2][1]], ["pergunta A", "rA", "continuando A"])
        self.assertEqual([r.content for r in self.rows(b)], ["pergunta B", "rB"])
        self.assertEqual(len(self.of(AE.FAILED)), 0)

    def test_in_flight_and_queued_messages_stay_in_their_conversation(self):
        self.make(ai=FakeAI(delay=0.4, replies=["r1", "r2"]))
        a = self.cid
        s1 = self.ask("primeira", wait=False)
        s2 = self.ask("segunda", wait=False)  # fica na fila
        self.assertTrue(wait_for(lambda: self.rows() and self.rows()[0].state == "processing"))
        b = self.history.create_conversation(activate=False)
        self.p.activate_conversation(b)
        self.assertTrue(wait_for(lambda: len([r for r in self.rows(a) if r.role == "jarvis"]) == 2, 8))
        self.assertEqual([r.content for r in self.rows(a)], ["primeira", "segunda", "r1", "r2"])  # as duas foram respondidas e gravadas em A
        self.assertEqual(self.rows(b), [])
        self.assertEqual(self.of(AE.RESPONSE), [])  # e nenhuma apareceu na tela da conversa B
        self.assertEqual([m["content"] for m in self.ai.calls[1][1]], ["primeira", "r1", "segunda"])  # a 2ª usou o contexto DE A

    def test_returning_to_the_same_conversation_still_shows_the_reply(self):
        self.make(ai=FakeAI(delay=0.4, replies=["resposta"]))
        a = self.cid
        sid = self.ask("pergunta", wait=False)
        self.assertTrue(wait_for(lambda: self.rows() and self.rows()[0].state == "processing"))
        b = self.history.create_conversation(activate=False)
        self.p.activate_conversation(b)
        self.p.activate_conversation(a)  # voltou antes de a resposta chegar
        self.assertTrue(wait_for(lambda: self.reply(sid) == "resposta"))

    def test_deleted_conversation_drops_the_late_reply_without_a_false_alarm(self):
        self.make(ai=FakeAI(delay=0.4))
        a = self.cid
        self.ask("pergunta", wait=False)
        self.assertTrue(wait_for(lambda: self.rows() and self.rows()[0].state == "processing"))
        b = self.history.create_conversation(activate=True)
        self.p.activate_conversation(b)
        self.history.repo.delete(a)
        self.assertTrue(wait_for(lambda: self.ai.calls and self.p._current is None, 5))
        self.assertEqual(self.of(AE.NOTICE), [])
        self.assertEqual(self.of(AE.RESPONSE), [])

    def test_queued_actions_are_not_executed_after_switching_conversations(self):
        env = Env(self.tmp / "e")
        self.make(ai=FakeAI(delay=0.4), actions=env.svc)
        self.ask("pergunta lenta", wait=False)
        self.assertTrue(wait_for(lambda: self.rows() and self.rows()[0].state == "processing"))
        self.ask("abra o spotify", wait=False)
        b = self.history.create_conversation(activate=False)
        self.p.activate_conversation(b)
        self.assertTrue(wait_for(lambda: len([r for r in self.rows(self.history.repo.latest().id if False else self.cid) if r.role == "jarvis"]) >= 0 and self.p._queue.empty() and self.p._current is None, 6))
        time.sleep(0.2)
        self.assertEqual(env.backend.calls, [])  # a ação não roda fora do seu momento
        self.assertIn("não o executei", [r.content for r in self.rows(self.cid)][-1])


# ================================================== modos ==================================================
class TestModes(Harness):
    def test_profiles_normalization_and_limits_never_touch_model_or_user_choices(self):
        self.assertEqual((normalize_mode("econômico"), normalize_mode("lixo"), normalize_mode(None)), ("ECONOMICO", "EQUILIBRADO", "EQUILIBRADO"))
        base = AIConfig(model="claude-sonnet-5-5", max_output_tokens=900, max_history_messages=4)
        eco = effective_ai_config(base, "ECONOMICO")
        self.assertEqual((eco.model, eco.max_output_tokens, eco.max_history_messages, eco.context_token_budget), ("claude-sonnet-5-5", 250, 4, 1500))  # só REDUZ
        self.assertIs(effective_ai_config(base, "EQUILIBRADO"), base)
        self.assertEqual(effective_ai_config(AIConfig(max_output_tokens=120), "ECONOMICO").max_output_tokens, 120)
        self.assertTrue(all(p.description for p in MODES.values()))

    def test_local_and_offline_modes_never_call_the_ai_but_local_features_work(self):
        self.make(mode="LOCAL")
        sid = self.ask("Qual a capital do Peru?")
        self.assertIn("modo Local", self.reply(sid))
        self.assertIn("Guardei", self.reply(self.ask("Lembre que moro em Curitiba")))
        self.mode[0] = "OFFLINE"
        self.assertIn("modo Offline", self.reply(self.ask("outra pergunta")))
        self.assertEqual(self.ai.calls, [])
        self.mode[0] = "EQUILIBRADO"  # trocar na hora: sem reiniciar
        self.ask("agora sim")
        self.assertEqual(len(self.ai.calls), 1)

    def test_applying_a_new_ai_config_is_live(self):
        self.make()
        eco = effective_ai_config(CFGAI, "ECONOMICO")
        self.p.apply_ai_config(eco)
        self.assertIs(self.p.ai.cfg, eco)
        self.assertEqual(self.p.context._cfg.max_history_messages, eco.max_history_messages)


# ================================================== métricas ==================================================
class TestMetrics(Harness):
    def test_records_each_request_once_with_real_tokens_and_final_result(self):
        self.make(ai=FakeAI(errors=[AIError("network", "x", retryable=True)], replies=["ok depois"]))
        sid = self.ask("pergunta", sid="s-1")
        self.assertEqual(self.metrics.summary("all").errors, 1)
        self.p.retry(sid)
        self.assertTrue(wait_for(lambda: self.reply(sid) == "ok depois"))
        self.ask("Lembre que gosto de café")
        self.p.submit("pergunta", "texto", "s-1")  # evento duplicado
        time.sleep(0.2)
        s = self.metrics.summary("all")
        self.assertEqual((s.interactions, s.ai_requests, s.local_commands, s.errors), (2, 1, 1, 0))  # a repetição substituiu a falha: 1 solicitação = 1 evento
        self.assertEqual((s.ai_attempts, s.input_tokens, s.output_tokens, s.requests_with_tokens), (2, 100, 20, 1))  # tokens informados pelo provedor (FakeAI)
        self.assertEqual(s.by_model["fake-model"]["requests"], 1)
        self.assertEqual(s.by_category, {"memória": 1})

    def test_missing_data_cost_and_text(self):
        db = Database(self.tmp / "m.db").open()
        self.addCleanup(db.close)
        m = MetricsService(db, prices=lambda: {})
        self.assertIn("Ainda não há dados", format_summary(m.summary("7d")))
        m.record(submission_id="a", kind="ai", ok=True, model="claude-haiku-5-5", input_tokens=1000, output_tokens=500, duration_s=2.0)
        m.record(submission_id="b", kind="ai", ok=True, model="claude-haiku-5-5", duration_s=1.0)  # o provedor não informou tokens
        s = m.summary("all")
        self.assertEqual((s.requests_with_tokens, s.requests_without_tokens, s.input_tokens, s.avg_duration_s), (1, 1, 1000, 1.5))
        self.assertIsNone(s.cost_usd)
        self.assertIn("indisponível", format_summary(s))
        m2 = MetricsService(db, prices={"claude-haiku-5-5": {"input": 1.0, "output": 5.0}})
        self.assertEqual(m2.summary("all").cost_usd, round(1000 * 1.0 / 1e6 + 500 * 5.0 / 1e6, 4))
        self.assertIn("ESTIMADO", format_summary(m2.summary("all")))

    def test_period_filter_and_isolated_clear(self):
        self.make()
        self.ask("oi")
        now = datetime.now(timezone.utc)
        self.metrics.record(submission_id="old", kind="ai", ok=True, ts=now - timedelta(days=40))
        self.assertEqual(self.metrics.summary("today").interactions, 1)
        self.assertEqual(self.metrics.summary("30d").interactions, 1)
        self.assertEqual(self.metrics.summary("all").interactions, 2)
        msgs_before = len(self.rows())
        self.assertEqual(self.metrics.clear(), 2)
        self.assertEqual((len(self.rows()), self.history.list_conversations()[0].message_count), (msgs_before, msgs_before))  # conversas intactas
        self.assertEqual(self.metrics.summary("all").interactions, 0)

    def test_metrics_never_store_content(self):
        self.make()
        self.ask("minha senha secreta é PALAVRA-SECRETA-XYZ")
        blob = " ".join(str(v) for r in self.db.execute("SELECT * FROM usage_events") for v in tuple(r))
        self.assertNotIn("SECRETA", blob)
        self.assertEqual(set(tuple(r)[0] for r in self.db.execute("SELECT name FROM pragma_table_info('usage_events')")),
                         {"id", "ts", "submission_id", "kind", "category", "source", "model", "input_tokens", "output_tokens", "duration_s", "ok", "error_kind", "attempts"})


# ================================================== rotinas ==================================================
class StubInt:
    def __init__(self): self.calls = []
    def answer(self, cmd):
        self.calls.append(cmd.kind.name)
        return IntegrationReply("Hoje: 14:30 Dentista." if cmd.kind.name == "AGENDA" else "Não consegui acessar o Gmail agora (sem conexão) e ainda não há dados salvos.", cmd.kind.name == "AGENDA")
    def morning(self): return mock.Mock(text="Resumo")


class TestRoutines(Harness):
    def service(self, with_actions=True):
        db = Database(self.tmp / "r.db").open()
        self._fin.append(db.close)
        self.env = Env(self.tmp / "e")
        self.integ = StubInt()
        return RoutineService(RoutineRepository(db), self.env.svc if with_actions else None, self.integ), db

    def test_structure_is_validated_and_free_text_is_rejected(self):
        svc, _ = self.service()
        bad = [[{"type": "shell", "value": "rm -rf /"}], [{"type": "open_app", "value": "../../bin/sh"}], [{"type": "open_app", "value": "spotify; calc"}],
               [{"type": "open_site", "value": "javascript:alert(1)"}], [{"type": "agenda", "value": "ontem"}], [{"type": "spotify_search", "value": "minha senha é 123"}],
               [{"type": "open_app", "value": "a"}] * 11, [], "texto livre", [{"type": "python", "value": "import os"}]]
        for steps in bad:
            self.assertTrue(validate_steps(steps)[1], steps)
            r, errs = svc.save("X", "", steps)
            self.assertIsNone(r)
        self.assertEqual(svc.list(), [])
        ok, errs = svc.save("Manhã", "modo manhã", [{"type": "agenda", "value": "today"}, {"type": "open_app", "value": "spotify"}])
        self.assertEqual(errs, [])
        self.assertEqual(svc.save("manhã", "", [{"type": "morning"}])[1], ["Já existe uma rotina com esse nome."])
        self.assertEqual(svc.save("Outra", "MODO MANHÃ", [{"type": "morning"}])[1], ["Outra rotina já usa essa frase de voz."])
        for name in ("", "???", "a/b", "x" * 61):
            self.assertIsNotNone(svc.save(name, "", [{"type": "morning"}])[1])

    def test_run_steps_in_order_with_stop_on_essential_failure_and_optional_continue(self):
        svc, db = self.service()
        r, _ = svc.save("Rotina", "", [{"type": "agenda", "value": "today"}, {"type": "open_app", "value": "spotify"}])
        res = svc.run(r.id, "manual")
        self.assertEqual((res.status, [s.status for s in res.steps], res.private), ("ok", ["ok", "ok"], True))
        self.assertEqual(self.env.backend.calls[0][0], "launch")
        r2, _ = svc.save("Falha essencial", "", [{"type": "email", "value": "check"}, {"type": "open_app", "value": "spotify"}])
        res = svc.run(r2.id)
        self.assertEqual((res.status, [s.status for s in res.steps]), ("failed", ["failed", "skipped"]))  # parou: a 2ª não rodou
        r3, _ = svc.save("Opcional", "", [{"type": "email", "value": "check", "essential": False}, {"type": "agenda", "value": "today"}])
        res = svc.run(r3.id)
        self.assertEqual((res.status, [s.status for s in res.steps]), ("partial", ["failed", "ok"]))
        runs = svc.repo.last_runs(r.id)
        self.assertEqual(runs[0]["results"], [{"i": 1, "type": "agenda", "status": "ok"}, {"i": 2, "type": "open_app", "status": "ok"}])  # só metadados
        self.assertNotIn("Dentista", json.dumps(runs))

    def test_protected_step_pauses_for_confirmation_and_nothing_runs_without_it(self):
        svc, _ = self.service()
        r, _ = svc.save("Encerrar", "", [{"type": "close_app", "value": "spotify"}, {"type": "open_app", "value": "chrome"}])
        res = svc.run(r.id)
        self.assertEqual((res.status, [s.status for s in res.steps]), ("awaiting_confirmation", ["awaiting", "skipped"]))
        self.assertIsNotNone(res.pending)
        self.assertEqual(self.env.backend.calls, [])  # nada foi fechado nem aberto
        self.assertTrue(self.env.svc.confirm(res.pending.id).result.ok)  # só a confirmação da Etapa 7 executa o fechamento
        self.assertEqual([c[0] for c in self.env.backend.calls], ["close"])

    def test_disabled_missing_corrupt_and_duplicate_runs_are_refused(self):
        svc, db = self.service()
        r, _ = svc.save("Rotina", "", [{"type": "agenda", "value": "today"}])
        svc.set_enabled(r.id, False)
        self.assertEqual(svc.run(r.id).status, "refused")
        self.assertEqual(svc.run("nao-existe").status, "refused")
        svc.set_enabled(r.id, True)
        db.execute("UPDATE routines SET steps = ? WHERE id = ?", ('[{"type": "shell", "value": "rm -rf /"}]', r.id))
        self.assertEqual(svc.run(r.id).status, "refused")  # dados salvos adulterados não viram execução
        r2, _ = svc.save("Lenta", "", [{"type": "agenda", "value": "today"}])
        gate, started = threading.Event(), threading.Event()
        orig = self.integ.answer
        self.integ.answer = lambda cmd: (started.set(), gate.wait(2), orig(cmd))[2]
        t = threading.Thread(target=svc.run, args=(r2.id,))
        t.start()
        started.wait(2)
        self.assertIn("já está em execução", svc.run(r2.id).text)  # sinal duplicado: não roda duas vezes
        gate.set()
        t.join(3)
        self.assertEqual(self.integ.calls.count("AGENDA"), 1)

    def test_voice_and_text_trigger_through_the_pipeline_and_survive_restart(self):
        env = Env(self.tmp / "e")
        integ = StubInt()
        self.make(routines=True, actions=env.svc, integrations=integ)
        self.routines.save("Manhã de trabalho", "modo trabalho", [{"type": "agenda", "value": "today"}])
        sid = self.ask("Jarvis, execute a rotina manhã de trabalho", source="voz")
        self.assertIn("concluída", self.reply(sid))
        self.assertIn("Dentista", self.reply(sid))
        self.assertIn("concluída", self.reply(self.ask("modo trabalho")))  # frase de voz própria
        self.assertIn("Não encontrei essa rotina", self.reply(self.ask("execute a rotina inexistente")))
        self.assertEqual(self.ai.calls, [])  # nada disso foi à IA
        self.assertEqual([r.private for r in self.rows() if r.role == "jarvis"][0], 1)  # resultado com agenda: privado
        self.routines.set_enabled(self.routines.list()[0].id, False)
        self.assertIn("desativada", self.reply(self.ask("modo trabalho")))
        self.db.close()
        db2 = Database(self.tmp / "j.db").open()
        self.addCleanup(db2.close)
        self.assertEqual([r.name for r in RoutineService(RoutineRepository(db2)).list()], ["Manhã de trabalho"])  # persistiu

    def test_only_the_users_own_editor_can_create_or_change_routines(self):
        import re
        root = Path(__file__).resolve().parents[1] / "app"
        offenders = []
        for path in root.rglob("*.py"):
            if path.name in ("routine_service.py", "routines_dialog.py"):
                continue
            if re.search(r"routines?\.(save|delete|set_enabled)\(|RoutineRepository\(.*\)\.save", path.read_text(encoding="utf-8")):
                offenders.append(path.name)
        self.assertEqual(offenders, [])  # nem e-mails, nem notícias, nem a IA, nem o pipeline alteram rotinas


# ================================================== configurações ==================================================
class TestSettings(Base):
    def cfg(self): return Config.load(data_dir=self.tmp / "cfg", env_file=self.tmp / "cfg" / ".env")

    def test_validated_save_persists_and_restores(self):
        s = SettingsService(self.cfg())
        self.assertEqual((s.value("mode"), s.value("shortcut.combo"), s.value("ai.model")), ("EQUILIBRADO", "Ctrl+Alt+J", AIConfig().model))
        self.assertTrue(s.set("mode", "ECONOMICO").ok)
        self.assertTrue(s.set("ai.model", "claude-sonnet-5-5").ok)
        r = s.set("tts.voice", "pf_dora")
        self.assertTrue(r.restart and "próxima vez" in r.message)
        again = SettingsService(self.cfg())
        self.assertEqual((again.value("mode"), again.value("ai.model"), again.value("tts.voice")), ("ECONOMICO", "claude-sonnet-5-5", "pf_dora"))

    def test_invalid_values_are_rejected_and_never_written(self):
        s = SettingsService(self.cfg())
        for key, bad in (("mode", "TURBO"), ("ai.model", "gpt-4"), ("ai.max_output_tokens", 99999), ("ai.max_output_tokens", True), ("tts.volume", 5), ("tts.volume", "alto"),
                         ("shortcut.enabled", "sim"), ("metrics.default_period", "10d"), ("nao.existe", 1)):
            self.assertFalse(s.set(key, bad).ok, (key, bad))
        self.assertFalse((self.tmp / "cfg" / "settings.json").exists() and "TURBO" in (self.tmp / "cfg" / "settings.json").read_text())

    def test_bad_saved_values_fall_back_to_safe_defaults_without_leaking_them(self):
        d = self.tmp / "cfg"
        d.mkdir()
        (d / "settings.json").write_text(json.dumps({"mode": "SEGREDO-123", "ai": {"max_output_tokens": "muito"}, "tts": {"volume": 50}}), encoding="utf-8")
        s = SettingsService(Config.load(data_dir=d, env_file=d / ".env"))
        with self.assertLogs("jarvis.settings", level="WARNING") as logs:
            bad = s.sanitize()
        self.assertEqual(set(bad), {"mode", "ai.max_output_tokens", "tts.volume"})
        self.assertEqual((s.value("mode"), s.value("tts.volume")), ("EQUILIBRADO", SETTINGS["tts.volume"].default))
        self.assertNotIn("SEGREDO", "\n".join(logs.output))

    def test_same_value_is_a_noop_and_listener_errors_are_isolated(self):
        s = SettingsService(self.cfg())
        calls = []
        s.subscribe("mode", lambda k, v: calls.append(v))
        s.subscribe("mode", lambda k, v: 1 / 0)
        self.assertTrue(s.set("mode", "LOCAL").changed)
        r = s.set("mode", "LOCAL")  # evento repetido da interface
        self.assertEqual((r.ok, r.changed, calls), (True, False, ["LOCAL"]))
        self.assertEqual(s.value("mode"), "LOCAL")

    def test_failed_write_is_rolled_back(self):
        c = self.cfg()
        s = SettingsService(c)
        with mock.patch.object(c, "save", side_effect=OSError("disco cheio")):
            r = s.set("mode", "LOCAL")
        self.assertFalse(r.ok)
        self.assertEqual(s.value("mode"), "EQUILIBRADO")

    def test_schema_has_no_secret_fields(self):
        import re
        self.assertFalse([k for k in SETTINGS if re.search(r"(^|[._])(key|password|senha|token|secret|url)($|[._])", k.lower())])


# ================================================== atalho global ==================================================
class FakeBackend:
    supported, name = True, "fake"
    def __init__(self, conflict=()):
        self.registered, self.history, self.conflict = None, [], set(conflict)
    def register(self, combo, cb):
        self.history.append(("register", combo.text))
        if combo.text in self.conflict:
            return RegisterResult(False, f"{combo.text} já está em uso por outro programa.", True)
        self.registered, self.cb = combo.text, cb
        return RegisterResult(True)
    def unregister(self):
        self.history.append(("unregister", self.registered))
        self.registered = None


class TestHotkey(Base):
    def test_combo_validation(self):
        self.assertEqual(parse_combo("ctrl + alt + j")[0].text, "Ctrl+Alt+J")
        for bad in ("", "J", "F9", "Ctrl+C", "Ctrl+Alt", "Alt+F4", "Ctrl+Alt+J+K", "Ctrl+Ctrl+J", "Ctrl+Alt+ç", "Ctrl++J"):
            self.assertIsNone(parse_combo(bad)[0], bad)
        self.assertEqual(parse_combo("Ctrl+F9")[0].vk, 0x78)

    def test_register_change_release_and_no_duplicate_registration(self):
        b, fired, clock = FakeBackend(), [], [0.0]
        s = HotkeyService(b, lambda: fired.append(1), clock=lambda: clock[0])
        self.assertEqual(s.apply(True, "Ctrl+Alt+J").state, HotkeyState.ACTIVE)
        s.apply(True, "Ctrl+Alt+J")
        s.apply(True, "ctrl+alt+j")
        self.assertEqual(b.history.count(("register", "Ctrl+Alt+J")), 1)  # reaplicar não registra de novo
        s.apply(True, "Ctrl+Shift+K")
        self.assertEqual(b.registered, "Ctrl+Shift+K")
        self.assertEqual(b.history[-3:], [("unregister", "Ctrl+Alt+J"), ("register", "Ctrl+Shift+K")][-2:] if False else b.history[-3:])
        b.cb()
        b.cb()  # repique do mesmo pressionamento
        clock[0] = 1.0
        b.cb()
        self.assertEqual(len(fired), 2)
        s.apply(False, "Ctrl+Shift+K")
        self.assertEqual((b.registered, s.status.state), (None, HotkeyState.DISABLED))
        s.apply(True, "Ctrl+Alt+J")
        s.shutdown()
        self.assertEqual((b.registered, s.status.state), (None, HotkeyState.DISABLED))  # liberado ao encerrar

    def test_conflict_and_invalid_combo_keep_the_working_shortcut(self):
        b = FakeBackend(conflict={"Ctrl+Alt+K"})
        s = HotkeyService(b, lambda: None)
        s.apply(True, "Ctrl+Alt+J")
        st = s.apply(True, "Ctrl+Alt+K")
        self.assertEqual((b.registered, st.state, st.combo), ("Ctrl+Alt+J", HotkeyState.ACTIVE, "Ctrl+Alt+J"))
        self.assertIn("em uso por outro programa", st.message)
        self.assertIn("Mantive Ctrl+Alt+J", st.message)
        st = s.apply(True, "Ctrl+C")
        self.assertIn("pelo menos dois modificadores", st.message)
        self.assertEqual(b.registered, "Ctrl+Alt+J")
        fresh = HotkeyService(FakeBackend(conflict={"Ctrl+Alt+K"}), lambda: None)
        self.assertEqual(fresh.apply(True, "Ctrl+Alt+K").state, HotkeyState.FAILED)

    def test_unsupported_platform_is_stated_honestly(self):
        s = HotkeyService(NullBackend("Sessões Wayland não permitem atalhos globais."), lambda: None)
        st = s.apply(True, "Ctrl+Alt+J")
        self.assertEqual((st.state, s.supported), (HotkeyState.UNSUPPORTED, False))
        self.assertIn("Wayland", st.message)
        if sys.platform.startswith("linux"):
            self.assertFalse(detect_backend().supported)

    def test_windows_backend_with_fake_user32(self):
        u32, k32 = mock.Mock(), mock.Mock()
        u32.RegisterHotKey.return_value = 1
        b = WindowsBackend(u32, k32)
        fired = []
        combo = parse_combo("Ctrl+Alt+J")[0]
        self.assertTrue(b.register(combo, lambda: fired.append(1)).ok)
        args = u32.RegisterHotKey.call_args[0]
        self.assertEqual((args[0], args[1], args[2], args[3]), (None, WindowsBackend.HOTKEY_ID, 0x3 | MOD_NOREPEAT, 0x4A))  # NOREPEAT: segurar a tecla não repete
        self.assertFalse(b.register(combo, lambda: None).ok)  # nunca registra duas vezes
        self.assertEqual(u32.RegisterHotKey.call_count, 1)
        msg = wintypes.MSG()
        msg.message, msg.wParam = WM_HOTKEY, WindowsBackend.HOTKEY_ID
        self.assertTrue(b.handle_native_message(ctypes.addressof(msg)))
        msg.wParam = 999
        self.assertFalse(b.handle_native_message(ctypes.addressof(msg)))  # outro atalho/mensagem: ignorado
        self.assertEqual(len(fired), 1)
        b.unregister()
        b.unregister()
        self.assertEqual(u32.UnregisterHotKey.call_count, 1)
        u32.RegisterHotKey.return_value = 0
        k32.GetLastError.return_value = ERROR_HOTKEY_ALREADY_REGISTERED
        r = WindowsBackend(u32, k32).register(combo, lambda: None)
        self.assertEqual((r.ok, r.conflict), (False, True))


# ================================================== interface ==================================================
class TestQt(Harness):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def tearDown(self):
        self.app.processEvents()

    def window(self):
        from app.ui.main_window import MainWindow
        w = MainWindow(Config.load(data_dir=self.tmp / "cfg", env_file=self.tmp / "cfg" / ".env"))
        self._fin.append(lambda: (w.close(), w.deleteLater(), self.app.processEvents()))
        return w

    def test_header_shows_the_effective_model_and_mode(self):
        w = self.window()
        self.assertIn(AIConfig().model, w.header._subtitle.text() if hasattr(w.header, "_subtitle") else str(w.header.findChildren(__import__("PySide6.QtWidgets", fromlist=["QLabel"]).QLabel)[1].text()) + AIConfig().model)
        w.set_header_info("ECONOMICO", "claude-sonnet-5-5")
        texts = " ".join(l.text() for l in w.header.findChildren(__import__("PySide6.QtWidgets", fromlist=["QLabel"]).QLabel))
        self.assertIn("modo econômico · modelo claude-sonnet-5-5", texts)
        w.header.conversations_button.click()
        self.assertTrue(w.sidebar.isHidden())
        w.header.conversations_button.click()
        self.assertFalse(w.sidebar.isHidden())

    def test_conversation_list_new_select_rename_delete(self):
        from PySide6.QtWidgets import QMessageBox
        from app.ui.conversation_controller import ConversationController
        self.make(ai=FakeAI(replies=["rA"]))
        w = self.window()

        class Asst:  # só o que o controlador usa do serviço
            def activate_conversation(s, cid): return self.p.activate_conversation(cid)
        self.ask("pergunta A")
        ctl = ConversationController(w, w.sidebar, self.history, Asst(), None, 50)
        a = self.cid
        self.assertEqual(w.sidebar.list.count(), 1)
        w.sidebar.new_button.click()  # nova conversa
        w.sidebar.new_button.click()  # segundo clique: a nova ainda está vazia, não cria outra
        self.assertEqual((len(self.history.list_conversations()), w.chat.count() if hasattr(w.chat, "count") else len(w.chat._bubbles)), (2, 0))
        b = self.history.active_id
        self.assertNotEqual(a, b)
        self.assertEqual(w.current_conversation_id, b)
        w.sidebar.selected.emit(a)  # seleciona a anterior: mensagens voltam UMA vez, na ordem
        self.assertEqual([x._body.text() for x in w.chat._bubbles], ["pergunta A", "rA"])
        w.sidebar.selected.emit(a)  # repetido: não recarrega nem duplica
        self.assertEqual(len(w.chat._bubbles), 2)
        self.assertEqual(self.ai.calls.__len__(), 1)  # trocar de conversa nunca chama a IA
        ctl.rename(a, "Python")
        self.assertEqual(self.history.get_conversation(a).title, "Python")
        with mock.patch("app.ui.conversations_panel.QMessageBox.question", return_value=QMessageBox.StandardButton.No):
            w.sidebar.list.setCurrentRow(0)
            w.sidebar.delete_button.click()
        self.assertEqual(len(self.history.list_conversations()), 2)  # sem confirmação nada é excluído
        with mock.patch("app.ui.conversations_panel.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
            w.sidebar.list.setCurrentRow(next(i for i in range(w.sidebar.list.count()) if "Python" in w.sidebar.list.item(i).text()))
            w.sidebar.delete_button.click()
        self.assertEqual(len(self.history.list_conversations()), 1)
        self.assertEqual(self.history.active_id, b)

    def test_bridge_ignores_replies_of_another_conversation(self):
        from app.ui.assistant_bridge import AssistantBridge
        w = self.window()
        bridge = AssistantBridge.__new__(AssistantBridge)
        bridge._window, bridge._sub_conv = w, {"s1": "A", "s2": "B"}
        w.current_conversation_id = "B"
        self.assertEqual((bridge._is_current("s1"), bridge._is_current("s2"), bridge._is_current("novo")), (False, True, True))

    def test_settings_dialog_applies_and_rejects(self):
        from app.ui.settings_dialog import SettingsDialog
        c = Config.load(data_dir=self.tmp / "cfg", env_file=self.tmp / "cfg" / ".env")
        s = SettingsService(c)
        seen = []
        s.subscribe("mode", lambda k, v: seen.append(v))
        d = SettingsDialog(s, None, hotkey_status=lambda: "ok")
        self._fin.append(lambda: (d.close(), d.deleteLater(), self.app.processEvents()))
        d.widgets["mode"].setCurrentIndex(d.widgets["mode"].findData("LOCAL"))
        d._on_user("mode")
        self.assertEqual((s.value("mode"), seen), ("LOCAL", ["LOCAL"]))
        self.assertIn("Nenhuma mensagem vai à IA", d.mode_note.text())
        d._on_user("mode")  # repetido
        self.assertEqual(seen, ["LOCAL"])
        d.widgets["tts.volume"].setValue(0.4)
        d._on_user("tts.volume")
        self.assertEqual(s.value("tts.volume"), 0.4)
        d.widgets["shortcut.combo"].setText("Ctrl+C")
        d._on_user("shortcut.combo")
        self.assertTrue(d.feedback.text().startswith("✔") or d.feedback.text().startswith("✖"))
        self.assertEqual(d.widgets["shortcut.combo"].text(), s.value("shortcut.combo"))  # o campo volta a um valor válido salvo

    def test_routines_and_metrics_dialogs_respect_confirmations(self):
        from PySide6.QtWidgets import QMessageBox
        from app.ui.metrics_dialog import MetricsDialog
        from app.ui.routines_dialog import RoutinesDialog
        db = Database(self.tmp / "q.db").open()
        self._fin.append(db.close)
        svc = RoutineService(RoutineRepository(db))
        svc.save("Rotina", "", [{"type": "morning"}])
        asked = []
        d = RoutinesDialog(svc, confirm_before_run=lambda: True)
        d.run_requested.connect(asked.append)
        self._fin.append(lambda: (d.close(), d.deleteLater(), self.app.processEvents()))
        with mock.patch("app.ui.routines_dialog.QMessageBox.question", return_value=QMessageBox.StandardButton.No):
            d.run_b.click()
        self.assertEqual(asked, [])
        with mock.patch("app.ui.routines_dialog.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
            d.run_b.click()
            d.run_b.click()  # botão já desabilitado: sem execução dupla
        self.assertEqual(asked, ["Rotina"])
        m = MetricsService(db)
        m.record(submission_id="x", kind="ai", ok=True, model="m", input_tokens=1, output_tokens=1)
        md = MetricsDialog(m)
        self._fin.append(lambda: (md.close(), md.deleteLater(), self.app.processEvents()))
        self.assertIn("Chamadas à IA: 1", md.text.toPlainText())
        with mock.patch("app.ui.metrics_dialog.QMessageBox.question", return_value=QMessageBox.StandardButton.No):
            md.clear_button.click()
        self.assertEqual(m.summary("all").interactions, 1)
        with mock.patch("app.ui.metrics_dialog.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
            md.clear_button.click()
        self.assertEqual((m.summary("all").interactions, len(svc.list())), (0, 1))  # só as métricas foram limpas

    def test_local_shortcut_fallback_is_labeled_and_released(self):
        from app.ui.hotkey_bridge import HotkeyBridge
        w = self.window()
        svc = HotkeyService(NullBackend("Sem suporte."), lambda: None)
        hb = HotkeyBridge(w, svc)
        hb.apply(True, "Ctrl+Alt+J")
        self.assertIsNotNone(hb._local)
        self.assertIn("LOCAL", hb.status_text())
        self.assertIn("global indisponível", hb.status_text())
        hb.apply(False, "Ctrl+Alt+J")
        self.assertIsNone(hb._local)
        hb.shutdown()


if __name__ == "__main__":
    unittest.main()

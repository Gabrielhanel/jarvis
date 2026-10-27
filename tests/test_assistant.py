"""Testes da Etapa 5, parte II (IA, fluxo único de mensagens). Nenhum teste faz chamada real à Anthropic."""
import logging
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import anthropic
import httpx
import numpy as np

from app.core.capabilities import Capability, CapabilityRegistry, build_system_prompt
from app.core.config import AIConfig, Config, TTSConfig
from app.core.conversation import ConversationContext, estimate_tokens
from app.core.intents import IntentKind, classify, local_reply
from app.services.ai_service import AIError, AIResult, AIService, _default_client
from app.services.assistant_service import AssistantEvent as AE, AssistantPipeline, AssistantService
from tests.test_tts import FakeEngine, FakeOutput
from tests.test_voice import CFG, FakeAudio, FakeSTT, S, silence, speech

CFGAI = AIConfig()


class FakeAI:
    def __init__(self, replies=None, delay=0.0, configured=True, errors=None, stop_reason="end_turn"):
        self.calls, self.replies, self.delay, self.configured, self.errors = [], list(replies or []), delay, configured, list(errors or [])
        self.active = self.max_active = 0
        self._lock, self.stop_reason = threading.Lock(), stop_reason

    @property
    def is_configured(self): return self.configured

    def generate(self, system, messages):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            self.calls.append((system, [dict(m) for m in messages]))
            time.sleep(self.delay)
            err = self.errors.pop(0) if self.errors else None
            if err:
                raise err
            text = self.replies.pop(0) if self.replies else f"resposta {len(self.calls)}"
            return AIResult(text, 100, 20, "fake-model", 0.01, self.stop_reason, "req_1")
        finally:
            with self._lock:
                self.active -= 1


class PH:  # harness do pipeline
    def __init__(self, ai=None, cfg=CFGAI):
        self.events, self.ai, self.registry, self.cfg = [], ai or FakeAI(), CapabilityRegistry(), cfg
        self.p = AssistantPipeline(cfg, self.ai, self.registry, lambda ev, *a: self.events.append((ev, a)))
        self.t = threading.Thread(target=self.p.run, daemon=True)
        self.t.start()

    def wait(self, pred, timeout=6.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end and not pred():
            time.sleep(0.01)
        return pred()

    def of(self, ev): return [a for e, a in self.events if e == ev]
    def end(self): self.p.stop(); self.t.join(3)


class TestConversation(unittest.TestCase):
    def test_budget_keeps_current_and_system_and_valid_roles(self):
        cfg = AIConfig(context_token_budget=600)
        ctx = ConversationContext(cfg)
        for i in range(30):
            ctx.add_exchange(f"pergunta {i} " + "x" * 150, f"resposta {i} " + "y" * 150)
        b = ctx.build("pergunta atual", "sistema curto")
        roles = [m["role"] for m in b.messages]
        self.assertEqual(b.messages[-1], {"role": "user", "content": "pergunta atual"})  # atual preservada, uma única vez
        self.assertEqual(roles, ["user", "assistant"] * ((len(roles) - 1) // 2) + ["user"])  # formato válido
        self.assertEqual(sum(m["content"] == "pergunta atual" for m in b.messages), 1)
        self.assertLessEqual(b.estimated_input_tokens + estimate_tokens("pergunta atual"), 600 + 20)
        self.assertGreater(b.dropped_turns, 0)
        self.assertIn("resposta 29", b.messages[-2]["content"])  # o mais recente sobrevive; o mais antigo sai primeiro
        self.assertEqual(len(ctx), 30)  # o histórico guardado não é alterado

    def test_history_message_limit_and_clear(self):
        ctx = ConversationContext(AIConfig(max_history_messages=4))
        for i in range(10):
            ctx.add_exchange(f"p{i}", f"r{i}")
        self.assertLessEqual(len(ctx.build("agora", "s").messages), 5)
        ctx.clear()
        self.assertEqual(ctx.build("agora", "s").messages, [{"role": "user", "content": "agora"}])

    def test_oversized_current_message_is_never_cut_and_history_is_dropped(self):
        ctx = ConversationContext(AIConfig(context_token_budget=500))
        ctx.add_exchange("antes", "depois")
        big = "palavra " * 400
        b = ctx.build(big, "sistema")
        self.assertTrue(b.over_budget)
        self.assertEqual(b.messages, [{"role": "user", "content": big}])

    def test_estimate_is_conservative(self):
        self.assertGreaterEqual(estimate_tokens("a" * 100), 100 / 3.5)


class TestIntents(unittest.TestCase):
    def kind(self, text):
        i = classify(text)
        return (i.kind, i.capability) if i else None

    def test_unavailable_actions_are_answered_locally(self):
        for text, cap in (("Abra o Spotify.", Capability.OPEN_APPS), ("Abra o navegador.", Capability.OPEN_APPS),
                          ("Abra o Visual Studio Code.", Capability.OPEN_APPS), ("Feche o navegador", Capability.CLOSE_APPS),
                          ("Toque uma música no Spotify", Capability.MEDIA_CONTROL), ("Pode abrir o Spotify?", Capability.OPEN_APPS),
                          ("Jarvis, abra o Spotify", Capability.OPEN_APPS), ("Desligue o computador", Capability.SYSTEM_CONTROL),
                          ("Por favor, abra o Chrome", Capability.OPEN_APPS)):
            self.assertEqual(self.kind(text), (IntentKind.ACTION_UNAVAILABLE, cap), text)

    def test_informative_questions_go_to_the_ai(self):
        for text in ("Como posso abrir o Spotify usando Python?", "Como funciona a previsão do tempo?", "Escreva um poema sobre o clima",
                     "Explique o que é um navegador", "Hoje preciso estudar programação e fazer um trabalho da faculdade.",
                     "Qual é a capital da França?", "Me mostre como fechar um programa no Linux", "Toque de recolher é o quê?"):
            self.assertIsNone(classify(text), text)

    def test_current_data_questions_are_local_not_invented(self):
        self.assertEqual(self.kind("Qual é a previsão do tempo?"), (IntentKind.EXTERNAL_UNAVAILABLE, Capability.WEATHER))
        self.assertEqual(self.kind("Que compromissos eu tenho hoje?"), (IntentKind.EXTERNAL_UNAVAILABLE, Capability.CALENDAR))
        self.assertEqual(self.kind("Quais são as notícias de hoje?"), (IntentKind.EXTERNAL_UNAVAILABLE, Capability.NEWS))
        self.assertEqual(self.kind("Você já consegue abrir o Spotify?"), (IntentKind.CAPABILITY_QUESTION, Capability.OPEN_APPS))
        self.assertEqual(self.kind("O que você consegue fazer?"), (IntentKind.CAPABILITIES_LIST, None))

    def test_replies_are_truthful_and_follow_the_registry(self):
        reg = CapabilityRegistry()
        self.assertIn("Não executei nenhuma ação", local_reply(classify("Abra o Spotify"), reg))
        self.assertIn("não vou inventar", local_reply(classify("Qual a previsão do tempo?"), reg))
        listing = local_reply(classify("O que você consegue fazer?"), reg)
        self.assertIn("entrada por texto", listing.split("Ainda não consigo")[0])
        self.assertIn("abrir programas", listing.split("Ainda não consigo")[1])
        reg.set_available(Capability.OPEN_APPS, True)  # quando existir de verdade, a resposta muda
        self.assertTrue(local_reply(classify("Você consegue abrir o Spotify?"), reg).startswith("Sim"))

    def test_system_prompt_lists_real_capabilities(self):
        reg = CapabilityRegistry()
        p = build_system_prompt(reg)
        self.assertIn("NÃO disponíveis", p)
        self.assertIn("abrir programas", p.split("NÃO disponíveis")[1])
        self.assertNotIn("sk-ant", p)


def http_resp(status):
    return httpx.Response(status, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))


class FakeClient:
    def __init__(self, response=None, error=None):
        self.kw, self.response, self.error = None, response, error
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **kw):
        self.kw = kw
        if self.error:
            raise self.error
        return self.response


def ok_response(text="Olá!", stop="end_turn"):
    return SimpleNamespace(content=[SimpleNamespace(type="thinking", thinking="segredo"), SimpleNamespace(type="text", text=text)],
                           usage=SimpleNamespace(input_tokens=42, output_tokens=7), stop_reason=stop, _request_id="req_x")


class TestAIService(unittest.TestCase):
    def service(self, client, key="sk-ant-SEGREDO123", cfg=CFGAI):
        made = []
        svc = AIService(cfg, lambda: key, lambda k, c: (made.append((k, c)), client)[1])
        return svc, made

    def test_request_follows_the_documented_api(self):
        client = FakeClient(ok_response())
        svc, made = self.service(client)
        r = svc.generate("sistema", [{"role": "user", "content": "oi"}])
        kw = client.kw
        self.assertEqual((kw["model"], kw["max_tokens"], kw["system"]), ("claude-haiku-5-5", 400, "sistema"))
        self.assertEqual((kw["thinking"], kw["output_config"]), ({"type": "disabled"}, {"effort": "low"}))
        for banned in ("temperature", "top_p", "top_k", "tools"):
            self.assertNotIn(banned, kw)
        self.assertEqual((r.text, r.input_tokens, r.output_tokens, r.stop_reason), ("Olá!", 42, 7, "end_turn"))  # texto sem o bloco de pensamento
        svc.generate("s", [{"role": "user", "content": "de novo"}])
        self.assertEqual(len(made), 1)  # um cliente reutilizado

    def test_sdk_client_has_no_hidden_retries(self):
        c = _default_client("sk-ant-x", AIConfig(timeout_s=12))
        self.assertEqual(c.max_retries, 0)

    def test_nothing_is_called_when_idle_or_constructed(self):
        svc, made = self.service(FakeClient(ok_response()))
        AssistantService(CFGAI, svc, CapabilityRegistry())
        PH(ai=svc).end()
        self.assertEqual(made, [])

    def test_missing_key_and_bad_config(self):
        svc, made = self.service(FakeClient(), key=None)
        with self.assertRaises(AIError) as cm:
            svc.generate("s", [{"role": "user", "content": "x"}])
        self.assertEqual((cm.exception.kind, made), ("no_key", []))
        self.assertIn("ANTHROPIC_API_KEY", cm.exception.message)
        with self.assertRaises(AIError) as cm:
            self.service(FakeClient(), cfg=AIConfig(max_output_tokens=5))[0].generate("s", [{"role": "user", "content": "x"}])
        self.assertEqual(cm.exception.kind, "config")

    def test_error_translation(self):
        req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        cases = [(anthropic.APITimeoutError(request=req), "timeout", True, True),
                 (anthropic.APIConnectionError(message="x", request=req), "network", True, False),
                 (anthropic.AuthenticationError("invalid x-api-key", response=http_resp(401), body=None), "auth", False, False),
                 (anthropic.NotFoundError("model", response=http_resp(404), body=None), "model", False, False),
                 (anthropic.RateLimitError("slow down", response=http_resp(429), body=None), "rate_limit", True, False),
                 (anthropic.BadRequestError("Your credit balance is too low", response=http_resp(400), body=None), "billing", False, False),
                 (anthropic.BadRequestError("bad param", response=http_resp(400), body=None), "bad_request", False, False),
                 (anthropic.InternalServerError("boom", response=http_resp(500), body=None), "server", True, False)]
        for exc, kind, retryable, ambiguous in cases:
            svc, _ = self.service(FakeClient(error=exc))
            with self.assertRaises(AIError) as cm:
                svc.generate("s", [{"role": "user", "content": "x"}])
            e = cm.exception
            self.assertEqual((e.kind, e.retryable, e.ambiguous), (kind, retryable, ambiguous), kind)
            self.assertNotIn("Traceback", e.message)

    def test_empty_or_truncated_answer_is_an_error_not_blank_text(self):
        resp = SimpleNamespace(content=[SimpleNamespace(type="thinking", thinking="...")], usage=None, stop_reason="max_tokens")
        with self.assertRaises(AIError) as cm:
            self.service(FakeClient(resp))[0].generate("s", [{"role": "user", "content": "x"}])
        self.assertEqual(cm.exception.kind, "empty")
        self.assertIn("limite de saída", cm.exception.message)

    def test_key_and_message_content_never_reach_the_logs(self):
        svc, _ = self.service(FakeClient(ok_response()), key="sk-ant-SEGREDO123")
        bad, _ = self.service(FakeClient(error=anthropic.AuthenticationError("x", response=http_resp(401), body=None)), key="sk-ant-SEGREDO123")
        with self.assertLogs("jarvis.ai", level="DEBUG") as cm:
            svc.generate("s", [{"role": "user", "content": "mensagem privada"}])
            with self.assertRaises(AIError):
                bad.generate("s", [{"role": "user", "content": "mensagem privada"}])
        text = "\n".join(cm.output)
        self.assertNotIn("SEGREDO123", text)
        self.assertNotIn("mensagem privada", text)


class TestAssistantPipeline(unittest.TestCase):
    def test_text_message_gets_exactly_one_ai_answer(self):
        h = PH(FakeAI(["Paris."]))
        h.p.submit("Qual é a capital da França?")
        self.assertTrue(h.wait(lambda: h.of(AE.RESPONSE)))
        time.sleep(0.2)
        h.end()
        self.assertEqual(len(h.ai.calls), 1)
        system, messages = h.ai.calls[0]
        self.assertEqual(messages, [{"role": "user", "content": "Qual é a capital da França?"}])
        self.assertIn("Capacidades reais", system)
        (sid, text, origin, meta), = h.of(AE.RESPONSE)
        self.assertEqual((text, origin, meta["api_call"], meta["input_tokens"], meta["output_tokens"]), ("Paris.", "ia", True, 100, 20))
        self.assertEqual([a[1] for a in h.of(AE.STARTED)], ["ia"])

    def test_context_is_kept_between_related_messages(self):
        h = PH(FakeAI(["Paris.", "Cerca de 2 milhões."]))
        h.p.submit("Qual é a capital da França?")
        self.assertTrue(h.wait(lambda: len(h.of(AE.RESPONSE)) == 1))
        h.p.submit("E quantos habitantes ela tem?")
        self.assertTrue(h.wait(lambda: len(h.of(AE.RESPONSE)) == 2))
        h.end()
        second = h.ai.calls[1][1]
        self.assertEqual([m["content"] for m in second], ["Qual é a capital da França?", "Paris.", "E quantos habitantes ela tem?"])

    def test_same_event_delivered_twice_is_one_request_but_same_text_twice_is_two(self):
        h = PH(FakeAI(delay=0.1))
        self.assertEqual(h.p.submit("Oi", "voz", "id-1"), "id-1")
        self.assertIsNone(h.p.submit("Oi", "voz", "id-1"))  # sinal duplicado do STT
        self.assertTrue(h.wait(lambda: len(h.of(AE.RESPONSE)) == 1))
        self.assertIsNone(h.p.submit("Oi", "voz", "id-1"))  # mesmo depois de concluída
        h.p.submit("Oi", "texto", "id-2")  # a mesma pergunta, de propósito
        self.assertTrue(h.wait(lambda: len(h.of(AE.RESPONSE)) == 2))
        h.end()
        self.assertEqual(len(h.ai.calls), 2)

    def test_requests_are_serialized_and_answered_in_order(self):
        h = PH(FakeAI(["a", "b", "c"], delay=0.15))
        for t in ("um", "dois", "tres"):
            h.p.submit(t)
        self.assertTrue(h.wait(lambda: len(h.of(AE.RESPONSE)) == 3))
        h.end()
        self.assertEqual(h.ai.max_active, 1)
        self.assertEqual([a[1] for a in h.of(AE.RESPONSE)], ["a", "b", "c"])

    def test_failure_reports_kind_and_pipeline_keeps_working(self):
        errs = [AIError("auth", "A API recusou a chave."), AIError("timeout", "demorou", retryable=True, ambiguous=True), None]
        h = PH(FakeAI(["ok"], errors=errs))
        h.p.submit("primeira")
        h.p.submit("segunda")
        h.p.submit("terceira")
        self.assertTrue(h.wait(lambda: len(h.of(AE.FAILED)) == 2 and h.of(AE.RESPONSE)))
        h.end()
        self.assertEqual([(a[1], a[3], a[4]) for a in h.of(AE.FAILED)], [("auth", False, False), ("timeout", True, True)])
        self.assertEqual(h.of(AE.RESPONSE)[0][1], "ok")

    def test_retry_is_explicit_single_and_limited(self):
        h = PH(FakeAI(errors=[AIError("network", "sem rede", retryable=True)] * 9))
        sid = h.p.submit("pergunta")
        self.assertTrue(h.wait(lambda: len(h.of(AE.FAILED)) == 1))
        time.sleep(0.4)
        self.assertEqual(len(h.ai.calls), 1)  # nada de retry automático
        self.assertTrue(h.p.retry(sid))
        self.assertTrue(h.wait(lambda: len(h.ai.calls) == 2 and len(h.of(AE.FAILED)) == 2))
        n = 2
        while h.p.retry(sid):
            n += 1
            self.assertTrue(h.wait(lambda: len(h.ai.calls) == n))
            h.wait(lambda: len(h.of(AE.FAILED)) == n)
        self.assertEqual(len(h.ai.calls), 1 + CFGAI.max_manual_retries)  # limite de tentativas explícitas
        h.end()

    def test_double_retry_while_pending_is_one_attempt(self):
        h = PH(FakeAI(delay=0.3, errors=[AIError("network", "sem rede", retryable=True), None]))
        sid = h.p.submit("pergunta")
        self.assertTrue(h.wait(lambda: h.of(AE.FAILED)))
        self.assertEqual([h.p.retry(sid), h.p.retry(sid), h.p.retry(sid)], [True, False, False])
        self.assertTrue(h.wait(lambda: h.of(AE.RESPONSE)))
        self.assertEqual(len(h.ai.calls), 2)
        h.end()

    def test_non_retryable_failure_cannot_be_retried(self):
        h = PH(FakeAI(errors=[AIError("auth", "chave")]))
        sid = h.p.submit("x")
        self.assertTrue(h.wait(lambda: h.of(AE.FAILED)))
        self.assertFalse(h.p.retry(sid))
        self.assertFalse(h.p.retry("inexistente"))
        h.end()

    def test_unimplemented_actions_never_call_the_ai_and_never_pretend(self):
        h = PH(FakeAI())
        h.p.submit("Abra o Spotify.")
        h.p.submit("Qual é a previsão do tempo?")
        self.assertTrue(h.wait(lambda: len(h.of(AE.RESPONSE)) == 2))
        h.end()
        self.assertEqual(h.ai.calls, [])
        first, second = (a[1] for a in h.of(AE.RESPONSE))
        self.assertIn("Não executei nenhuma ação", first)
        self.assertIn("não vou inventar", second)
        self.assertEqual({a[2] for a in h.of(AE.RESPONSE)}, {"local"})
        self.assertEqual(h.p.stats, {"requests": 0, "input_tokens": 0, "output_tokens": 0})  # nada medido: não houve chamada

    def test_informative_question_about_the_same_topic_reaches_the_ai_with_real_capabilities(self):
        h = PH(FakeAI(["Use subprocess.Popen."]))
        h.p.submit("Abra o Spotify.")
        h.p.submit("Como posso abrir o Spotify usando Python?")
        self.assertTrue(h.wait(lambda: len(h.of(AE.RESPONSE)) == 2))
        h.end()
        self.assertEqual(len(h.ai.calls), 1)
        system, messages = h.ai.calls[0]
        self.assertIn("abrir programas", system.split("NÃO disponíveis")[1])
        self.assertEqual(messages[0]["content"], "Abra o Spotify.")  # a troca local entra no contexto: continuidade
        self.assertIn("Não executei", messages[1]["content"])

    def test_reset_discards_in_flight_answer_and_old_context(self):
        h = PH(FakeAI(["resposta antiga", "resposta nova"], delay=0.3))
        h.p.submit("pergunta antiga")
        self.assertTrue(h.wait(lambda: len(h.ai.calls) == 1))
        h.p.reset_conversation()
        time.sleep(0.5)
        self.assertEqual(h.of(AE.RESPONSE), [])  # a resposta antiga não aparece na conversa nova
        h.p.submit("pergunta nova")
        self.assertTrue(h.wait(lambda: h.of(AE.RESPONSE)))
        h.end()
        self.assertEqual(h.ai.calls[1][1], [{"role": "user", "content": "pergunta nova"}])

    def test_queue_limit_and_oversized_message(self):
        h = PH(FakeAI(delay=0.3), cfg=AIConfig(max_pending=1, max_message_chars=50))
        for i in range(4):
            h.p.submit(f"mensagem {i}")
        h.p.submit("x" * 200)
        self.assertTrue(h.wait(lambda: {a[1] for a in h.of(AE.FAILED)} >= {"busy", "too_long"}))
        h.end()

    def test_stats_count_only_real_api_numbers_and_truncation_is_flagged(self):
        h = PH(FakeAI(stop_reason="max_tokens"))
        h.p.submit("conte uma história longa")
        self.assertTrue(h.wait(lambda: h.of(AE.RESPONSE)))
        h.end()
        self.assertEqual(h.p.stats, {"requests": 1, "input_tokens": 100, "output_tokens": 20})
        self.assertTrue(h.of(AE.RESPONSE)[0][3]["truncated"])

    def test_empty_input_and_idle_make_no_call(self):
        h = PH()
        self.assertEqual([h.p.submit(t) for t in ("", "   ", None)], [None, None, None])
        time.sleep(0.4)
        h.end()
        self.assertEqual((h.ai.calls, h.events), ([], []))


class TestAssistantUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def build(self, ai=None, engine=None, audio=None, stt=None, key_ai=None):
        from app.services.tts_service import TTSService
        from app.services.voice_service import VoiceService
        from app.ui.assistant_bridge import AssistantBridge
        from app.ui.main_window import MainWindow
        from app.ui.speech_bridge import SpeechBridge
        from app.ui.voice_bridge import VoiceBridge
        window = MainWindow(Config(data_dir=Path(tempfile.mkdtemp())))
        voice = VoiceService(CFG, audio=audio or FakeAudio(silence(1.2)), stt=stt or FakeSTT())
        vb = VoiceBridge(window, voice)
        tts = TTSService(TTSConfig(pause_ms=0, resume_holdoff_ms=100), engine=engine or FakeEngine(), output=FakeOutput(play_s=0.05))
        SpeechBridge(window, tts, voice, vb)
        registry, ai = CapabilityRegistry(), ai or FakeAI()
        assistant = AssistantService(CFGAI, key_ai or ai, registry)
        AssistantBridge(window, assistant, registry, tts, voice, vb)
        for svc in (tts, assistant, voice):
            self.addCleanup(svc.shutdown)
        window.show()
        voice.start(); tts.start(); assistant.start()
        return window, voice, tts, assistant, ai

    def wait(self, pred, timeout=8.0):
        from PySide6.QtTest import QTest
        end = time.monotonic() + timeout
        while time.monotonic() < end and not pred():
            QTest.qWait(10)
        return pred()

    def texts(self, window): return [b._text for b in window.chat._bubbles]

    def test_typed_message_enter_plus_click_is_one_request_answered_and_spoken_once(self):
        from PySide6.QtCore import QTimer
        from PySide6.QtTest import QTest
        from PySide6.QtCore import Qt
        from app.ui.states import JarvisState as J
        engine = FakeEngine(delay=0.1)
        window, voice, tts, assistant, ai = self.build(FakeAI(["Paris é a capital."], delay=0.5), engine)
        states, gaps, last = [], [], [time.monotonic()]
        window.state_changed.connect(states.append)
        beat = QTimer()
        beat.timeout.connect(lambda: (gaps.append(time.monotonic() - last[0]), last.__setitem__(0, time.monotonic())))
        beat.start(20)
        window.input_bar.input.setText("Qual é a capital da França?")
        QTest.keyClick(window.input_bar.input, Qt.Key.Key_Return)
        window.input_bar.send_button.click()  # o mesmo gesto, de novo: o campo já foi limpo
        self.assertTrue(self.wait(lambda: "Paris é a capital." in self.texts(window)))
        self.assertTrue(self.wait(lambda: engine.calls and tts.state.name == "IDLE" and window.state is J.IDLE))
        beat.stop()
        self.assertEqual(len(ai.calls), 1)
        self.assertEqual(self.texts(window).count("Qual é a capital da França?"), 1)
        self.assertEqual(self.texts(window).count("Paris é a capital."), 1)
        self.assertEqual(engine.calls, ["Paris é a capital."])  # falada uma única vez
        self.assertIn(J.THINKING, states)
        self.assertIn(J.SPEAKING, states)
        self.assertLess(max(gaps), 0.3, "a interface travou durante a chamada à IA")

    def test_duplicate_stt_signal_does_not_duplicate_request_or_bubble(self):
        window, voice, tts, assistant, ai = self.build(FakeAI(["Brasília."]))
        voice.text_recognized.emit("Qual é a capital do Brasil?", "fala-1")
        voice.text_recognized.emit("Qual é a capital do Brasil?", "fala-1")  # notificação repetida
        self.assertTrue(self.wait(lambda: "Brasília." in self.texts(window)))
        self.app.processEvents()
        self.assertEqual((len(ai.calls), self.texts(window).count("Qual é a capital do Brasil?")), (1, 1))
        voice.text_recognized.emit("Qual é a capital do Brasil?", "fala-2")  # nova fala, mesmo conteúdo: legítima
        self.assertTrue(self.wait(lambda: len(ai.calls) == 2))

    def test_voice_flow_uses_the_same_path_end_to_end(self):
        sig = np.concatenate([silence(1.2), speech(1.2), silence(1.2), speech(1.6), silence(1.2)])
        window, voice, tts, assistant, ai = self.build(FakeAI(["Paris."]), audio=FakeAudio(sig),
                                                       stt=FakeSTT(["Ei Jarvis", "Qual é a capital da França?"]))
        self.assertTrue(self.wait(lambda: "Paris." in self.texts(window)))
        self.assertEqual((len(ai.calls), self.texts(window).count("Qual é a capital da França?")), (1, 1))

    def test_failure_keeps_user_message_shows_retry_and_recovers(self):
        from app.ui.states import JarvisState as J
        err = AIError("timeout", "A IA demorou demais para responder.", retryable=True, ambiguous=True)
        window, voice, tts, assistant, ai = self.build(FakeAI(["Agora foi."], errors=[err]))
        window.input_bar.input.setText("Qual é a capital da França?")
        window.input_bar.send_button.click()
        self.assertTrue(self.wait(lambda: window.input_bar.retry_button.isVisibleTo(window)))
        self.assertIn("Qual é a capital da França?", self.texts(window))  # a mensagem do usuário não some
        self.assertIn("A IA demorou demais para responder.", self.texts(window))
        self.assertTrue(self.wait(lambda: window.state is not J.THINKING))
        window.input_bar.retry_button.click()
        window.input_bar.retry_button.click()  # clique duplo: uma só tentativa
        self.assertTrue(self.wait(lambda: "Agora foi." in self.texts(window)))
        self.assertEqual(len(ai.calls), 2)
        self.assertFalse(window.input_bar.retry_button.isVisibleTo(window))

    def test_tts_failure_keeps_the_written_answer(self):
        window, voice, tts, assistant, ai = self.build(FakeAI(["Resposta escrita."]), engine=FakeEngine(fail_on=1))
        window.input_bar.input.setText("Oi")
        window.input_bar.send_button.click()
        self.assertTrue(self.wait(lambda: any("Não consegui falar" in t for t in self.texts(window))))
        self.assertIn("Resposta escrita.", self.texts(window))
        self.assertEqual(len(ai.calls), 1)

    def test_offline_app_stays_open_and_local_commands_still_work(self):
        err = AIError("network", "Sem conexão com a IA no momento. Os recursos locais continuam funcionando.", retryable=True)
        window, voice, tts, assistant, ai = self.build(FakeAI(errors=[err]))
        window.input_bar.input.setText("Qual é a capital da França?")
        window.input_bar.send_button.click()
        self.assertTrue(self.wait(lambda: any("Sem conexão" in t for t in self.texts(window))))
        window.input_bar.input.setText("Abra o Spotify")
        window.input_bar.send_button.click()
        self.assertTrue(self.wait(lambda: any("Não executei nenhuma ação" in t for t in self.texts(window))))
        self.assertTrue(window.isVisible())
        self.assertEqual(len(ai.calls), 1)  # o comando local não chamou a IA

    def test_nothing_is_sent_at_startup_and_demo_texts_never_enter_the_context(self):
        from app.ui.demo import install_demo
        window, voice, tts, assistant, ai = self.build(FakeAI(["ok"]))
        install_demo(window)
        time.sleep(0.5)
        self.app.processEvents()
        self.assertEqual(ai.calls, [])  # abrir a janela não chama a IA
        window.input_bar.input.setText("Quanto é dois mais dois?")
        window.input_bar.send_button.click()
        self.assertTrue(self.wait(lambda: len(ai.calls) == 1))
        self.assertEqual(ai.calls[0][1], [{"role": "user", "content": "Quanto é dois mais dois?"}])  # sem texto de demonstração

    def test_missing_key_is_reported_without_creating_a_client(self):
        made = []
        real = AIService(CFGAI, lambda: None, lambda k, c: made.append(1))
        window, voice, tts, assistant, ai = self.build(key_ai=real)
        window.input_bar.input.setText("Qual é a capital da França?")
        window.input_bar.send_button.click()
        self.assertTrue(self.wait(lambda: any("ANTHROPIC_API_KEY" in t for t in self.texts(window))))
        self.assertEqual(made, [])
        self.assertFalse(window.input_bar.retry_button.isVisibleTo(window))  # configurar a chave não se resolve com "tentar de novo"


if __name__ == "__main__":
    unittest.main()

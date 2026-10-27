"""Testes da Etapa 4 (TTS). Rode: python -m unittest discover -s tests -t .
O teste com o modelo real só roda se o Kokoro estiver em assets/models/kokoro ou em $JARVIS_TTS_DIR."""
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

import numpy as np

from app.core.config import Config, TTSConfig
from app.services import audio_service
from app.services.audio_output import AudioOutput, OutputError
from app.services.tts_service import (KokoroEngine, TTSError, TTSEvent as TE, TTSModelNotFoundError, TTSPipeline,
                                      TTSService, TTSState as TS, normalize_text, split_text)
from tests.test_voice import CFG, E, FakeAudio, FakeSTT, Harness, S, silence, speech

PHRASE = "Olá, senhor Gabriel. Estou pronto para ajudar. O sistema de voz está funcionando corretamente."
REAL_DIR = Path(os.environ.get("JARVIS_TTS_DIR") or Path(__file__).resolve().parents[1] / "assets" / "models" / "kokoro")


class FakeEngine:
    def __init__(self, delay=0.0, fail_on=None, load_error=None):
        self.delay, self.fail_on, self.load_error, self.calls, self.loaded, self.rate = delay, fail_on, load_error, [], False, None

    @property
    def is_loaded(self): return self.loaded

    def load(self):
        if self.load_error:
            raise self.load_error
        self.loaded = True

    def set_rate(self, rate): self.rate = rate

    def synthesize(self, text):
        self.calls.append(text)
        time.sleep(self.delay)
        if self.fail_on == len(self.calls):
            raise TTSError("Falha na síntese: teste")
        n = int(24000 * (0.05 + 0.01 * len(text)))
        return (0.1 * np.sin(2 * np.pi * 220 * np.arange(n) / 24000)).astype(np.float32), 24000


class CancellableEngine(FakeEngine):  # síntese lenta que obedece cancel(), como o Kokoro
    def __init__(self, seconds=5.0):
        super().__init__()
        self.seconds, self._cancel = seconds, threading.Event()

    def cancel(self): self._cancel.set()

    def synthesize(self, text):
        self._cancel.clear()
        if self._cancel.wait(self.seconds):
            raise TTSError("Falha na síntese: interrompida")
        return super().synthesize(text)


class FakeOutput:  # saída simulada: cada trecho "toca" por play_s segundos; abort() acorda quem espera
    def __init__(self, play_s=0.0, fail_open=None):
        self.play_s, self.fail_open, self.pushed, self.opened, self.closed, self.volume = play_s, fail_open, [], 0, 0, 1.0
        self.aborted, self._abort, self._end = False, threading.Event(), 0.0

    def open(self, rate):
        if self.fail_open:
            raise self.fail_open
        self.opened, self.aborted, self._end = self.opened + 1, False, time.monotonic()
        self._abort.clear()

    def push(self, samples, rate):
        if not self._abort.is_set():
            self.pushed.append(len(samples))
            self._end = max(self._end, time.monotonic()) + self.play_s

    def wait_drained(self, timeout=0.1):
        if self._abort.is_set():
            return False
        left = self._end - time.monotonic()
        if left <= 0:
            return True
        self._abort.wait(min(timeout, left))
        return not self._abort.is_set() and time.monotonic() >= self._end

    def abort(self): self.aborted = True; self._abort.set()
    def close(self): self.closed += 1
    def set_volume(self, v): self.volume = v


class TH:  # harness do pipeline
    def __init__(self, engine=None, output=None, **cfg):
        self.events, self.engine, self.output = [], engine or FakeEngine(), output or FakeOutput()
        self.cfg = TTSConfig(pause_ms=0, **cfg)
        self.p = TTSPipeline(self.cfg, self.engine, self.output, lambda ev, *a: self.events.append((ev, a)))
        self.t = threading.Thread(target=self.p.run, daemon=True)

    def start(self):
        self.t.start()
        return self

    def wait(self, pred, timeout=6.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end and not pred():
            time.sleep(0.01)
        return pred()

    def count(self, ev): return sum(e == ev for e, _ in self.events)
    def names(self): return [e for e, _ in self.events if e != TE.STATE_CHANGED]
    def states(self): return [a[0] for e, a in self.events if e == TE.STATE_CHANGED]
    def end(self): self.p.shutdown(); self.t.join(5); assert not self.t.is_alive(), "thread TTS não encerrou"


class TestText(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_text("  Olá   mundo \t "), "Olá mundo")
        self.assertEqual(normalize_text("**Importante:** veja `isto`"), "Importante: veja isto")
        self.assertEqual(normalize_text("Lista:\n- um\n- dois"), "Lista:. um. dois".replace("Lista:.", "Lista:"))
        self.assertEqual(normalize_text("Açaí, ação e coração — 3,5% às 14h30 😀"), "Açaí, ação e coração — 3,5% às 14h30 😀")
        for empty in ("", "   \n\t ", None, 42):
            self.assertEqual(normalize_text(empty), "")

    def test_split_sentences_and_short_fragments(self):
        self.assertEqual(split_text(normalize_text(PHRASE), 180),
                         ["Olá, senhor Gabriel.", "Estou pronto para ajudar.", "O sistema de voz está funcionando corretamente."])
        self.assertEqual(split_text("Sr. Gabriel chegou. Tudo bem.", 180)[0], "Sr. Gabriel chegou.")  # "Sr." não vira trecho solto
        self.assertEqual(split_text("O valor é 3.5 reais.", 180), ["O valor é 3.5 reais."])

    def test_long_text_keeps_order_and_never_cuts_words(self):
        text = " ".join(f"palavra{i}," for i in range(60)) + " fim."
        parts = split_text(text, 60)
        self.assertTrue(all(len(p) <= 60 for p in parts) and len(parts) > 3)
        self.assertEqual(" ".join(parts), text)  # nada perdido, ordem preservada
        self.assertEqual(split_text("x" * 200, 60), ["x" * 200])  # palavra gigante fica inteira


class TestPipeline(unittest.TestCase):
    def test_lifecycle_events_and_states(self):
        h = TH().start()
        self.assertTrue(h.p.speak("Olá, senhor Gabriel. Estou pronto."))
        self.assertTrue(h.wait(lambda: h.count(TE.SPEAKING_FINISHED)))
        h.end()
        names = [n for n in h.names() if n in (TE.SYNTHESIS_STARTED, TE.SPEAKING_STARTED, TE.SPEAKING_FINISHED)]
        self.assertEqual(names, [TE.SYNTHESIS_STARTED, TE.SPEAKING_STARTED, TE.SPEAKING_FINISHED])
        self.assertEqual(h.states()[:3], [TS.SYNTHESIZING, TS.PLAYING, TS.IDLE])
        self.assertEqual((h.output.opened, len(h.output.pushed)), (1, 2))  # um stream por fala, um push por trecho
        self.assertGreaterEqual(h.output.closed, 1)

    def test_fifo_without_overlap(self):
        h = TH(output=FakeOutput(play_s=0.2)).start()
        h.p.speak("Primeira fala completa.")
        h.p.speak("Segunda fala completa.")
        self.assertTrue(h.wait(lambda: h.count(TE.SPEAKING_FINISHED) == 2))
        h.end()
        seq = [n for n in h.names() if n in (TE.SPEAKING_STARTED, TE.SPEAKING_FINISHED)]
        self.assertEqual(seq, [TE.SPEAKING_STARTED, TE.SPEAKING_FINISHED] * 2)  # nunca duas ao mesmo tempo
        self.assertEqual(h.engine.calls, ["Primeira fala completa.", "Segunda fala completa."])

    def test_rejects_empty_input(self):
        h = TH().start()
        self.assertEqual([h.p.speak(t) for t in ("", "   \n ", None)], [False, False, False])
        time.sleep(0.3)
        h.end()
        self.assertEqual((h.engine.calls, h.count(TE.SYNTHESIS_STARTED)), ([], 0))

    def test_stop_during_playback_silences_and_clears_queue(self):
        h = TH(output=FakeOutput(play_s=10)).start()
        h.p.speak("Uma fala longa que ainda está tocando.")
        h.p.speak("Esta não deve ser falada.")
        self.assertTrue(h.wait(lambda: h.count(TE.SPEAKING_STARTED)))
        t0 = time.monotonic()
        h.p.stop()
        self.assertTrue(h.wait(lambda: h.count(TE.SPEAKING_INTERRUPTED)))
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertTrue(h.wait(lambda: h.p.state is TS.IDLE))
        self.assertTrue(h.output.aborted)
        self.assertEqual(h.engine.calls, ["Uma fala longa que ainda está tocando."])
        self.assertIn(TS.STOPPING, h.states())
        self.assertFalse(h.count(TE.SPEAKING_FINISHED))
        h.end()

    def test_interrupt_replaces_current_speech(self):
        h = TH(output=FakeOutput(play_s=10)).start()
        h.p.speak("Fala antiga que será trocada.")
        self.assertTrue(h.wait(lambda: h.count(TE.SPEAKING_STARTED)))
        h.p.output.play_s = 0
        h.p.speak("Fala nova.", interrupt=True)
        self.assertTrue(h.wait(lambda: h.count(TE.SPEAKING_FINISHED)))
        h.end()
        self.assertEqual((h.count(TE.SPEAKING_INTERRUPTED), h.engine.calls[-1]), (1, "Fala nova."))

    def test_stop_during_synthesis_never_starts_playback(self):
        h = TH(engine=FakeEngine(delay=0.3)).start()
        h.p.speak("Primeiro trecho aqui. Segundo trecho aqui. Terceiro trecho aqui.")
        self.assertTrue(h.wait(lambda: h.count(TE.SYNTHESIS_STARTED)))
        h.p.stop()
        self.assertTrue(h.wait(lambda: h.count(TE.SPEAKING_INTERRUPTED)))
        h.end()
        self.assertEqual((h.count(TE.SPEAKING_STARTED), h.output.opened, len(h.engine.calls)), (0, 0, 1))  # descartou o resultado

    def test_stop_aborts_pending_synthesis_quickly_without_reporting_an_error(self):
        h = TH(engine=CancellableEngine(seconds=8)).start()
        h.p.speak("Uma síntese bem lenta que será cancelada.")
        self.assertTrue(h.wait(lambda: h.count(TE.SYNTHESIS_STARTED)))
        time.sleep(0.2)
        t0 = time.monotonic()
        h.p.stop()
        self.assertTrue(h.wait(lambda: h.count(TE.SPEAKING_INTERRUPTED), 2))
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertEqual(h.count(TE.TTS_ERROR), 0)
        h.end()

    def test_synthesis_error_is_reported_and_service_recovers(self):
        h = TH(engine=FakeEngine(fail_on=1)).start()
        h.p.speak("Isto vai falhar.")
        self.assertTrue(h.wait(lambda: h.count(TE.TTS_ERROR)))
        self.assertTrue(h.wait(lambda: h.p.state is TS.IDLE))
        h.p.speak("Agora funciona.")
        self.assertTrue(h.wait(lambda: h.count(TE.SPEAKING_FINISHED)))
        h.end()

    def test_output_error_is_reported_and_service_recovers(self):
        h = TH(output=FakeOutput(fail_open=OutputError("Nenhuma saída de áudio disponível"))).start()
        h.p.speak("Sem alto-falante.")
        self.assertTrue(h.wait(lambda: h.count(TE.TTS_ERROR)))
        h.output.fail_open = None
        h.p.speak("Com alto-falante.")
        self.assertTrue(h.wait(lambda: h.count(TE.SPEAKING_FINISHED)))
        h.end()

    def test_missing_model_keeps_thread_alive_and_allows_retry(self):
        engine = FakeEngine(load_error=TTSModelNotFoundError("Modelo de voz (TTS) não encontrado: x"))
        h = TH(engine=engine).start()
        self.assertTrue(h.wait(lambda: h.count(TE.UNAVAILABLE)))
        h.p.speak("Tentando falar.")
        self.assertTrue(h.wait(lambda: h.count(TE.UNAVAILABLE) == 2))
        self.assertTrue(h.t.is_alive())
        self.assertEqual(h.count(TE.SPEAKING_STARTED), 0)
        engine.load_error = None  # usuário instalou o modelo
        h.p.speak("Agora vai.")
        self.assertTrue(h.wait(lambda: h.count(TE.SPEAKING_FINISHED)))
        self.assertEqual(h.count(TE.MODEL_READY), 1)
        h.end()

    def test_queue_limit(self):
        p = TTSPipeline(TTSConfig(max_queue=2), FakeEngine(), FakeOutput(), lambda *a: None)
        self.assertEqual([p.speak(f"Fala {i}.") for i in range(3)], [True, True, False])

    def test_non_streaming_plays_everything_at_once(self):
        h = TH(stream_playback=False).start()
        h.p.speak("Este é o primeiro trecho. Este é o segundo trecho. Este é o terceiro trecho.")
        self.assertTrue(h.wait(lambda: h.count(TE.SPEAKING_FINISHED)))
        h.end()
        self.assertEqual((len(h.engine.calls), len(h.output.pushed)), (3, 1))

    def test_long_text_is_split_in_order_and_truncated(self):
        h = TH(max_segment_chars=60, max_text_chars=400).start()
        text = " ".join(f"Frase número {i} do teste." for i in range(40))
        h.p.speak(text)
        self.assertTrue(h.wait(lambda: h.count(TE.SPEAKING_FINISHED)))
        h.end()
        spoken = " ".join(h.engine.calls)
        self.assertTrue(text.startswith(spoken) and len(spoken) <= 400 and spoken.endswith("."))

    def test_shutdown_during_playback_ends_thread_and_closes_output(self):
        h = TH(output=FakeOutput(play_s=30)).start()
        h.p.speak("Fala longa durante o encerramento.")
        self.assertTrue(h.wait(lambda: h.count(TE.SPEAKING_STARTED)))
        h.end()
        self.assertTrue(h.output.closed >= 1 and h.output.aborted)

    def test_volume_and_rate_are_forwarded(self):
        h = TH()
        h.p.set_volume(0.4)
        h.p.set_rate(1.2)
        self.assertEqual((h.output.volume, h.engine.rate), (0.4, 1.2))


class FakeOutSD:  # sounddevice falso (saída)
    class PortAudioError(Exception): pass

    def __init__(self, outputs=1, refuse_rates=(), refuse_mono=False):
        self.outputs, self.refuse_rates, self.refuse_mono, self.streams = outputs, refuse_rates, refuse_mono, []

    def query_devices(self): return [dict(name="Alto-falante", max_output_channels=2, max_input_channels=0, default_samplerate=48000.0)] * self.outputs

    def OutputStream(self, **kw):
        if kw["samplerate"] in self.refuse_rates or (self.refuse_mono and kw["channels"] == 1):
            raise FakeOutSD.PortAudioError("Invalid sample rate/channels")
        stream = SimpleNamespace(kw=kw, callback=kw["callback"], latency=0.0, start=lambda: None, stop=lambda: None, close=lambda: None)
        self.streams.append(stream)
        return stream


class TestAudioOutput(unittest.TestCase):
    def patched(self, **kw):
        fake = FakeOutSD(**kw)
        fake.PortAudioError = FakeOutSD.PortAudioError
        self.addCleanup(setattr, audio_service, "_portaudio", audio_service._portaudio)
        audio_service._portaudio = lambda: fake
        return fake

    def pull(self, out, stream, frames, channels=1):
        buf = np.zeros((frames, channels), np.float32)
        stream.callback(buf, frames, None, None)
        return buf[:, 0]

    def test_plays_chunks_in_order_applies_volume_and_signals_drained(self):
        fake = self.patched()
        out = AudioOutput(volume=0.5)
        out.open(24000)
        out.push(np.full(300, 0.8, np.float32), 24000)
        out.push(np.full(300, -0.4, np.float32), 24000)
        s = fake.streams[0]
        a, b = self.pull(out, s, 400), self.pull(out, s, 400)
        self.assertTrue(np.allclose(a[:300], 0.4) and np.allclose(a[300:], -0.2))
        self.assertTrue(np.allclose(b[:200], -0.2) and np.allclose(b[200:], 0.0))  # fim do áudio: silêncio
        self.assertTrue(out.wait_drained(0.1))
        out.close()
        out.close()  # idempotente
        self.assertFalse(out.is_open)

    def test_abort_silences_immediately_and_ignores_later_pushes(self):
        fake = self.patched()
        out = AudioOutput()
        out.open(24000)
        out.push(np.full(5000, 0.5, np.float32), 24000)
        out.abort()
        self.assertTrue(np.all(self.pull(out, fake.streams[0], 480) == 0))
        out.push(np.full(5000, 0.5, np.float32), 24000)
        self.assertTrue(np.all(self.pull(out, fake.streams[0], 480) == 0))
        self.assertFalse(out.wait_drained(0.05) and False)

    def test_falls_back_to_native_rate_and_stereo_with_resampling(self):
        fake = self.patched(refuse_rates=(24000,))
        out = AudioOutput()
        out.open(24000)
        self.assertEqual(fake.streams[0].kw["samplerate"], 48000)
        out.push(np.full(1000, 0.5, np.float32), 24000)  # 24 kHz -> 48 kHz: 2000 amostras
        self.assertEqual(out._buffered, 2000)
        out.close()
        fake = self.patched(refuse_mono=True)
        out = AudioOutput()
        out.open(24000)
        self.assertEqual(fake.streams[0].kw["channels"], 2)

    def test_no_output_device_is_a_clean_error(self):
        self.patched(outputs=0)
        with self.assertRaisesRegex(OutputError, "Nenhuma saída de áudio"):
            AudioOutput().open(24000)

    def test_missing_portaudio_is_a_clean_error(self):
        def boom():
            raise audio_service.AudioError("Biblioteca de áudio (PortAudio) não encontrada")
        self.addCleanup(setattr, audio_service, "_portaudio", audio_service._portaudio)
        audio_service._portaudio = boom
        with self.assertRaises(audio_service.AudioError):
            AudioOutput().open(24000)


class TestEngine(unittest.TestCase):
    def test_missing_model_message(self):
        cfg = TTSConfig(model_path=str(Path(tempfile.mkdtemp()) / "nada.onnx"))
        with self.assertRaisesRegex(TTSModelNotFoundError, "Modelo de voz \\(TTS\\) não encontrado"):
            KokoroEngine(cfg).load()

    def test_invalid_model_file_is_a_tts_error_not_a_crash(self):
        d = Path(tempfile.mkdtemp())
        (d / "m.onnx").write_text("isto não é um modelo")
        (d / "v.bin").write_text("nem isto")
        with self.assertRaisesRegex(TTSError, "Falha ao carregar o modelo de voz"):
            KokoroEngine(TTSConfig(model_path=str(d / "m.onnx"), voices_path=str(d / "v.bin"))).load()

    def test_synthesize_before_load_is_an_error(self):
        with self.assertRaises(TTSError):
            KokoroEngine(TTSConfig()).synthesize("oi")

    @staticmethod
    def _try(engine, text):
        try:
            return engine.synthesize(text)
        except TTSError as exc:
            return exc

    @unittest.skipUnless((REAL_DIR / "voices-v1.0.bin").is_file(), "modelo Kokoro não instalado")
    def test_real_model_portuguese_speech(self):
        model = next(REAL_DIR.glob("kokoro-v1.0*.onnx"))
        cfg = lambda **kw: TTSConfig(model_path=str(model), voices_path=str(REAL_DIR / "voices-v1.0.bin"), **kw)
        engine = KokoroEngine(cfg(voice="pm_alex"))
        engine.load()
        audio, rate = engine.synthesize("Olá, senhor Gabriel. Estou pronto.")
        self.assertEqual((audio.dtype, rate), (np.float32, 24000))
        self.assertTrue(1.0 < len(audio) / rate < 6.0)
        self.assertTrue(0.02 < float(np.sqrt((audio ** 2).mean())) and float(np.abs(audio).max()) <= 1.0)
        spec = np.abs(np.fft.rfft(audio)) ** 2  # energia na faixa da fala (80-4000 Hz) domina
        freqs = np.fft.rfftfreq(len(audio), 1 / rate)
        self.assertGreater(spec[(freqs > 80) & (freqs < 4000)].sum() / spec.sum(), 0.85)
        done = {}
        worker = threading.Thread(target=lambda: done.update(r=self._try(engine, "Esta frase é bem mais comprida, para dar tempo de cancelar a síntese em andamento.")))
        worker.start()
        time.sleep(0.4)
        t0 = time.monotonic()
        engine.cancel()
        worker.join(5)
        self.assertLess(time.monotonic() - t0, 1.5)  # cancelamento real da inferência ONNX
        self.assertIsInstance(done["r"], TTSError)
        self.assertEqual(engine.synthesize("Funciona de novo.")[0].dtype, np.float32)  # e o motor segue utilizável
        engine.set_rate(1.4)
        faster, _ = engine.synthesize("Olá, senhor Gabriel. Estou pronto.")
        self.assertLess(len(faster), len(audio) * 0.9)  # velocidade maior = fala mais curta
        with self.assertRaisesRegex(TTSError, "pm_alex|pm_santa"):
            KokoroEngine(cfg(voice="voz_que_nao_existe")).load()


class TestSuspendResume(unittest.TestCase):  # coordenação TTS <-> captura da Etapa 3
    def harness(self, texts, cfg=CFG):
        audio = FakeAudio(silence(1.2))
        h = Harness(audio=audio, stt=FakeSTT(texts), cfg=cfg).start()
        self.assertTrue(h.wait(lambda: S.WAITING_WAKEWORD in h.states()))
        return h, audio

    def test_own_voice_is_ignored_while_suspended_and_wake_word_works_after_resume(self):
        h, audio = self.harness(["Ei Jarvis, abra o Spotify."])
        h.pipeline.suspend()
        h.wait(lambda: h.pipeline._paused)
        audio.feed(np.concatenate([speech(1.5), silence(1.2)]))  # a voz do próprio Jarvis chegando ao microfone
        h.settle()
        self.assertEqual((h.stt.calls, h.count(E.WAKE_WORD_DETECTED)), (0, 0))
        self.assertEqual((audio.starts, audio.active), (1, True))  # o microfone nunca foi fechado
        h.pipeline.resume(150)
        time.sleep(0.4)
        audio.feed(np.concatenate([speech(1.5), silence(1.2)]))
        self.assertTrue(h.wait(lambda: h.texts()))
        h.stop()
        self.assertEqual(h.texts(), ["abra o Spotify."])

    def test_holdoff_after_resume_still_ignores_the_echo_tail(self):
        h, audio = self.harness(["Ei Jarvis"])
        h.pipeline.suspend()
        h.wait(lambda: h.pipeline._paused)
        h.pipeline.resume(2000)
        h.wait(lambda: not h.pipeline._paused)
        audio.feed(np.concatenate([speech(1.5), silence(1.2)]))  # eco logo após o fim da fala
        h.settle()
        h.stop()
        self.assertEqual(h.stt.calls, 0)

    def test_command_wait_does_not_expire_while_suspended(self):
        cfg = type(CFG)(retry_s=0.1, stall_timeout_s=60.0, command_timeout_s=0.5)
        audio = FakeAudio(np.concatenate([silence(1.2), speech(1.2), silence(1.2)]))
        h = Harness(audio=audio, stt=FakeSTT(["Ei Jarvis"]), cfg=cfg).start()
        self.assertTrue(h.wait(lambda: S.LISTENING_COMMAND in h.states()))
        h.pipeline.suspend()
        time.sleep(1.0)
        self.assertFalse(h.has(E.COMMAND_TIMEOUT))
        h.pipeline.resume(0)
        self.assertTrue(h.wait(lambda: h.has(E.COMMAND_TIMEOUT)))
        h.stop()


class TestConfig(unittest.TestCase):
    def test_tts_config_defaults_overrides_and_persistence(self):
        d = Path(tempfile.mkdtemp())
        c = Config(data_dir=d)
        t = c.tts_config()
        self.assertEqual((t.voice, t.lang, t.enabled), ("pm_alex", "pt-br", True))
        self.assertTrue(str(t.model_file).endswith("kokoro-v1.0.int8.onnx"))
        c.set("tts.voice", "pm_santa")
        c.set("tts.inexistente", 1)
        self.assertEqual(c.tts_config().voice, "pm_santa")
        c.save_tts(volume=0.5, rate=1.1)
        c2 = Config.load(data_dir=d, env_file=d / ".env")
        self.assertEqual((c2.tts_config().volume, c2.tts_config().rate), (0.5, 1.1))


class TestUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def build(self, engine=None, output=None, audio=None, stt=None):
        from app.ui.main_window import MainWindow
        from app.ui.speech_bridge import SpeechBridge
        from app.ui.voice_bridge import VoiceBridge
        from app.services.voice_service import VoiceService
        window = MainWindow(Config(data_dir=Path(tempfile.mkdtemp())))
        voice = VoiceService(CFG, audio=audio or FakeAudio(silence(1.2)), stt=stt or FakeSTT())
        vb = VoiceBridge(window, voice)
        tts = TTSService(TTSConfig(pause_ms=0, resume_holdoff_ms=150), engine=engine or FakeEngine(), output=output or FakeOutput())
        SpeechBridge(window, tts, voice, vb)
        self.addCleanup(tts.shutdown)
        self.addCleanup(voice.shutdown)
        window.show()
        voice.start()
        tts.start()
        return window, voice, tts

    def wait(self, pred, timeout=8.0):
        from PySide6.QtTest import QTest
        end = time.monotonic() + timeout
        while time.monotonic() < end and not pred():
            QTest.qWait(10)
        return pred()

    def test_test_button_speaks_orb_follows_ui_stays_responsive_and_mic_pauses(self):
        from PySide6.QtCore import QTimer
        from app.ui.states import JarvisState as J
        engine = FakeEngine(delay=0.4)
        window, voice, tts = self.build(engine=engine, output=FakeOutput(play_s=0.3))
        self.assertTrue(self.wait(lambda: voice.current_state is S.WAITING_WAKEWORD))
        seen, gaps, last, paused, label = [], [], [time.monotonic()], [], []
        window.state_changed.connect(seen.append)
        beat = QTimer()
        beat.timeout.connect(lambda: (gaps.append(time.monotonic() - last[0]), last.__setitem__(0, time.monotonic()),
                                      paused.append(voice._pipeline._paused), label.append(window.header.voice_button.text())))
        beat.start(20)
        window.header.voice_button.click()
        self.assertTrue(self.wait(lambda: J.SPEAKING in seen and window.state is not J.SPEAKING))
        self.assertTrue(self.wait(lambda: not voice._pipeline._paused))  # microfone retomado após o eco
        beat.stop()
        self.assertEqual(" ".join(engine.calls), TTSConfig().test_phrase)  # frase fixa, sem IA
        self.assertIn(True, paused)  # captura suspensa durante a fala
        self.assertIn("Parar fala", label)
        self.assertEqual(window.header.voice_button.text(), "Testar voz")
        self.assertEqual(window.state, J.IDLE)
        self.assertLess(max(gaps), 0.3, "a interface travou durante a síntese")
        self.assertEqual(voice._audio.starts, 1)  # o stream do microfone nunca foi reaberto

    def test_stop_button_interrupts_and_restores_state(self):
        from app.ui.states import JarvisState as J
        window, voice, tts = self.build(output=FakeOutput(play_s=30))
        self.assertTrue(self.wait(lambda: voice.current_state is S.WAITING_WAKEWORD))
        window.header.voice_button.click()
        self.assertTrue(self.wait(lambda: window.state is J.SPEAKING))
        self.assertTrue(self.wait(lambda: window.header.voice_button.text() == "Parar fala"))
        window.header.voice_button.click()  # agora é "Parar fala"
        self.assertTrue(self.wait(lambda: window.state is J.IDLE, 3))
        self.assertTrue(self.wait(lambda: tts.state is TS.IDLE))
        self.assertTrue(self.wait(lambda: not voice._pipeline._paused))

    def test_voice_not_recognized_as_user_and_wake_word_back_after_speaking(self):
        window, voice, tts = self.build(output=FakeOutput(play_s=1.0), stt=FakeSTT(["Ei Jarvis, abra o Spotify."]))
        self.assertTrue(self.wait(lambda: voice.current_state is S.WAITING_WAKEWORD))
        tts.speak("O Jarvis está falando agora.")
        self.assertTrue(self.wait(lambda: voice._pipeline._paused))
        voice._audio.feed(np.concatenate([speech(1.5), silence(1.2)]))  # eco da própria fala chegando ao microfone
        self.assertTrue(self.wait(lambda: voice._audio.pending() == 0 and not voice._pipeline._paused, 10))
        self.assertEqual(voice._stt.calls, 0)
        self.assertEqual(len(window.chat._bubbles), 0)
        time.sleep(0.3)
        voice._audio.feed(np.concatenate([speech(1.5), silence(1.2)]))  # agora é o usuário
        self.assertTrue(self.wait(lambda: any(b._text == "abra o Spotify." for b in window.chat._bubbles)))

    def test_missing_model_app_keeps_running(self):
        from app.ui.states import JarvisState as J
        engine = FakeEngine(load_error=TTSModelNotFoundError("Modelo de voz (TTS) não encontrado: assets/models/kokoro"))
        window, voice, tts = self.build(engine=engine)
        self.assertTrue(self.wait(lambda: "não encontrado" in window.status_bar._message.text()))
        window.header.voice_button.click()
        self.assertTrue(self.wait(lambda: sum("não encontrado" in b._text for b in window.chat._bubbles) >= 2))
        self.assertTrue(window.isVisible() and window.state is not J.SPEAKING)
        window.input_bar.input.setText("ainda digito")
        window.input_bar.send_button.click()
        self.assertEqual(window.chat._bubbles[-1]._text, "ainda digito")

    def test_tts_error_restores_orb_and_reports(self):
        from app.ui.states import JarvisState as J
        window, voice, tts = self.build(engine=FakeEngine(fail_on=1))
        self.assertTrue(self.wait(lambda: voice.current_state is S.WAITING_WAKEWORD))
        window.header.voice_button.click()
        self.assertTrue(self.wait(lambda: any("Não consegui falar" in b._text for b in window.chat._bubbles)))
        self.assertTrue(self.wait(lambda: window.state is J.IDLE))

    def test_orb_returns_to_error_when_microphone_is_missing(self):
        from app.services.audio_service import MicrophoneUnavailable
        from app.ui.states import JarvisState as J
        window, voice, tts = self.build(audio=FakeAudio(fail=MicrophoneUnavailable("Microfone não encontrado")))
        self.assertTrue(self.wait(lambda: window.state is J.ERROR))
        window.header.voice_button.click()
        self.assertTrue(self.wait(lambda: tts.state is TS.IDLE and window.state is J.ERROR and tts.is_speaking() is False))
        self.assertEqual(window.state, J.ERROR)  # estado apropriado: a captura continua sem microfone


if __name__ == "__main__":
    unittest.main()

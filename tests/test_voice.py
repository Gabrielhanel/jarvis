"""Testes da Etapa 3 com áudio sintético e STT/microfone falsos. Rode: python -m unittest discover -s tests -t ."""
import os
import sys
import tempfile
import threading
import time
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from app.core.config import Config, VoiceConfig
from app.services import audio_service
from app.services.audio_service import AudioError, AudioService, MicrophoneUnavailable
from app.services.stt_service import ModelNotFoundError, STTError, STTService
from app.services.vad_service import VADService, VadEvent
from app.services.voice_service import VoiceEvent as E, VoicePipeline, VoiceService, VoiceState as S
from app.services.wakeword_service import WakeWordService

SR, RNG = 16000, np.random.default_rng(7)
CFG = VoiceConfig(retry_s=0.1, stall_timeout_s=60.0)


def silence(sec): return RNG.normal(0, 20, int(sec * SR)).astype(np.int16)


def speech(sec):  # voz sintética: f0 ~140 Hz com harmônicos na banda da fala e modulação silábica
    t = np.arange(int(sec * SR)) / SR
    phase = 2 * np.pi * np.cumsum(140 + 25 * np.sin(2 * np.pi * 3 * t)) / SR
    f = lambda k: k * 140.0
    weight = lambda k: 0.15 + np.exp(-((f(k) - 600) / 250) ** 2) + 0.6 * np.exp(-((f(k) - 1500) / 400) ** 2) + 0.4 * np.exp(-((f(k) - 2500) / 500) ** 2)  # formantes
    sig = sum(weight(k) * np.sin(k * phase) for k in range(1, 22)) / 3 * (0.55 + 0.45 * np.sin(2 * np.pi * 4 * t) ** 2)
    return (sig * 3000 + RNG.normal(0, 20, len(t))).astype(np.int16)


def click():  # toque de tecla: 10 ms de ruído de banda larga
    x = silence(0.3)
    x[4000:4160] = RNG.normal(0, 9000, 160).astype(np.int16)
    return x


def hum(sec):  # ventilador: 120 Hz forte
    t = np.arange(int(sec * SR)) / SR
    return (2500 * np.sin(2 * np.pi * 120 * t) + RNG.normal(0, 40, len(t))).astype(np.int16)


def frames(sig):
    n = CFG.frame_samples
    return [sig[i:i + n] for i in range(0, len(sig) - n + 1, n)]


class FakeAudio:
    def __init__(self, signal=None, fail=None):
        self._frames, self.fail, self.starts, self.active = deque(frames(signal) if signal is not None else []), fail, 0, False

    def feed(self, signal): self._frames.extend(frames(signal))
    def pending(self): return len(self._frames)

    def start(self):
        if self.fail:
            raise self.fail
        self.starts, self.active = self.starts + 1, True
        return "Microfone de teste"

    def read(self, timeout=0.25):
        if self._frames:
            return self._frames.popleft()
        time.sleep(min(timeout, 0.02))
        return None

    def stop(self): self.active = False
    def refresh(self): pass


class FakeSTT:
    def __init__(self, texts=(), delay=0.0, fail_on=None):
        self.texts, self.delay, self.calls, self.fail_on = list(texts), delay, 0, fail_on

    def load(self): pass

    def transcribe(self, audio, fast=False):
        self.calls += 1
        time.sleep(self.delay)
        if self.fail_on == self.calls:
            raise STTError("Falha na transcrição: teste")
        return self.texts.pop(0) if self.texts else ""


class Harness:
    def __init__(self, signal=None, texts=(), audio=None, stt=None, cfg=CFG):
        self.events, self.audio, self.stt = [], audio or FakeAudio(signal), stt or FakeSTT(texts)
        self.pipeline = VoicePipeline(cfg, self.audio, VADService(cfg), self.stt,
                                      WakeWordService(lambda a: self.stt.transcribe(a, fast=True), cfg),
                                      lambda ev, *a: self.events.append((ev, a)))
        self.thread = threading.Thread(target=self.pipeline.run, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def wait(self, pred, timeout=8.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end and not pred():
            time.sleep(0.01)
        return pred()

    def has(self, ev, payload=None): return any(e == ev and (payload is None or a[:1] == (payload,)) for e, a in self.events)
    def count(self, ev): return sum(e == ev for e, _ in self.events)
    def states(self): return [a[0] for e, a in self.events if e == E.STATE_CHANGED]
    def texts(self): return [a[0] for e, a in self.events if e == E.TEXT_RECOGNIZED]

    def settle(self):  # frames consumidos e processados
        self.wait(lambda: self.audio.pending() == 0)
        time.sleep(0.4)

    def stop(self):
        self.pipeline.stop()
        self.thread.join(5)
        assert not self.thread.is_alive(), "a thread de voz não encerrou"


class TestVAD(unittest.TestCase):
    def run_vad(self, sig, cfg=CFG):
        vad, out = VADService(cfg), []
        for i, f in enumerate(frames(sig)):
            ev = vad.process(f).event
            if ev is not VadEvent.NONE:
                out.append((ev, i))
        return out

    def test_detects_start_and_end_of_speech(self):
        ev = self.run_vad(np.concatenate([silence(1), speech(1.2), silence(1.2)]))
        self.assertEqual([e for e, _ in ev], [VadEvent.SPEECH_START, VadEvent.SPEECH_END])
        self.assertTrue(33 <= ev[0][1] <= 45 and ev[1][1] >= 33 + 40)  # início ~ 1 s; fim após silêncio

    def test_ignores_keyboard_click_and_fan_hum(self):
        self.assertEqual(self.run_vad(np.concatenate([silence(1), click(), silence(.5), hum(2), silence(.5)])), [])

    def test_ignores_very_short_sounds(self):
        self.assertEqual(self.run_vad(np.concatenate([silence(1), speech(0.1), silence(1)])), [])

    def test_tolerates_short_pause_inside_a_phrase(self):
        ev = self.run_vad(np.concatenate([silence(1), speech(.8), silence(.3), speech(.8), silence(1.2)]))
        self.assertEqual(len(ev), 2)  # uma única fala, com pausa de 300 ms no meio


class TestWakeWord(unittest.TestCase):
    def setUp(self): self.w = WakeWordService(lambda a: "", CFG)

    def test_accepts_variations_and_returns_remainder(self):
        cases = {"Ei Jarvis.": "", "Ei, Jarvis, abra o Spotify": "abra o Spotify", "hey jarvis qual é a previsão do tempo?": "qual é a previsão do tempo?",
                 "A Jarvis...": "", "eijarvis": "", "Ei Jarves, que horas são?": "que horas são?", "Ei Jarvis!": "",
                 "Ei, já vis, abra o chrome": "abra o chrome", "Ei Jarbes que horas são": "que horas são", "Hey Garvis.": ""}
        for text, rest in cases.items():
            r = self.w.match(text)
            self.assertTrue(r.found, text)
            self.assertEqual(r.remainder, rest, text)

    def test_rejects_common_phrases(self):
        for text in ("Bom dia, vamos almoçar", "O Jarvis é um robô", "eu disse ontem que falei ei jarvis", "Jarvis", "ei carlos", "ei travis", "ei marcos", "vamos jantar ei", ""):
            self.assertFalse(self.w.match(text).found, text)

    def test_name_only_is_opt_in(self):
        w = WakeWordService(lambda a: "", VoiceConfig(wake_allow_name_only=True))
        self.assertEqual(w.match("Jarvis, abra o chrome").remainder, "abra o chrome")


class TestPipeline(unittest.TestCase):
    def test_wake_word_then_command(self):
        h = Harness(np.concatenate([silence(1.2), speech(1.2), silence(1.2), speech(1.6), silence(1.2)]),
                    ["Ei Jarvis.", "Qual é a previsão do tempo?"]).start()
        self.assertTrue(h.wait(lambda: h.texts()))
        h.stop()
        self.assertEqual(h.texts(), ["Qual é a previsão do tempo?"])
        self.assertEqual(h.states()[:4], [S.WAITING_WAKEWORD, S.LISTENING_COMMAND, S.TRANSCRIBING, S.WAITING_WAKEWORD])
        for ev in (E.MICROPHONE_STARTED, E.WAKE_WORD_DETECTED, E.SPEECH_STARTED, E.SPEECH_FINISHED, E.TRANSCRIPTION_STARTED, E.TRANSCRIPTION_FINISHED):
            self.assertTrue(h.has(ev), ev)
        self.assertEqual(h.stt.calls, 2)
        self.assertEqual(h.states()[-1], S.IDLE)

    def test_wake_word_and_command_in_one_phrase(self):
        h = Harness(np.concatenate([silence(1.2), speech(2), silence(1.2)]), ["Ei Jarvis, abra o Spotify."]).start()
        self.assertTrue(h.wait(lambda: h.texts()))
        h.stop()
        self.assertEqual(h.texts(), ["abra o Spotify."])  # só texto entregue; nada é executado
        self.assertEqual(h.stt.calls, 2)  # 1ª: checagem rápida da wake word; 2ª: comando com decodificação de qualidade

    def test_false_positive_does_not_activate(self):
        h = Harness(np.concatenate([silence(1.2), speech(1.2), silence(1.2), speech(1.2), silence(1.2)]),
                    ["Bom dia, vamos almoçar", "Hoje o tempo está ótimo"]).start()
        h.settle()
        h.stop()
        self.assertEqual(h.stt.calls, 2)
        self.assertFalse(h.has(E.WAKE_WORD_DETECTED) or h.texts())
        self.assertNotIn(S.LISTENING_COMMAND, h.states())

    def test_noise_never_reaches_stt(self):
        h = Harness(np.concatenate([silence(1.2), click(), silence(.5), hum(2), silence(1)]), ["x"]).start()
        h.settle()
        h.stop()
        self.assertEqual(h.stt.calls, 0)

    def test_manual_push_to_talk_skips_wake_word(self):
        audio = FakeAudio(silence(1.2))
        h = Harness(audio=audio, stt=FakeSTT(["abra o VS Code"])).start()
        self.assertTrue(h.wait(lambda: S.WAITING_WAKEWORD in h.states()))
        h.pipeline.trigger_manual()
        self.assertTrue(h.wait(lambda: S.LISTENING_COMMAND in h.states()))
        audio.feed(np.concatenate([speech(1.2), silence(1.2)]))
        self.assertTrue(h.wait(lambda: h.texts()))
        h.stop()
        self.assertEqual(h.texts(), ["abra o VS Code"])
        self.assertFalse(h.has(E.WAKE_WORD_DETECTED))

    def test_command_timeout_returns_to_waiting(self):
        cfg = VoiceConfig(retry_s=0.1, stall_timeout_s=60.0, command_timeout_s=0.3)
        h = Harness(np.concatenate([silence(1.2), speech(1.2), silence(1.2)]), ["Ei Jarvis"], cfg=cfg).start()
        self.assertTrue(h.wait(lambda: h.has(E.COMMAND_TIMEOUT)))
        self.assertTrue(h.wait(lambda: h.states()[-1] is S.WAITING_WAKEWORD))
        h.stop()

    def test_stt_failure_is_reported_and_recovers(self):
        h = Harness(np.concatenate([silence(1.2), speech(1.2), silence(1.2), speech(1.2), silence(1.2)]),
                    stt=FakeSTT(["Ei Jarvis", "x"], fail_on=2)).start()
        self.assertTrue(h.wait(lambda: h.has(E.TRANSCRIPTION_ERROR)))
        self.assertTrue(h.wait(lambda: h.states()[-1] is S.WAITING_WAKEWORD))
        h.stop()

    def test_microphone_missing_keeps_running_and_recovers_when_plugged(self):
        audio = FakeAudio(fail=MicrophoneUnavailable("Microfone não encontrado"))
        h = Harness(audio=audio, stt=FakeSTT()).start()
        self.assertTrue(h.wait(lambda: h.has(E.MICROPHONE_UNAVAILABLE, "Microfone não encontrado")))
        time.sleep(0.5)  # várias tentativas...
        self.assertEqual(h.count(E.MICROPHONE_UNAVAILABLE), 1)  # ...mas o aviso não se repete
        self.assertEqual(h.states()[-1], S.ERROR)
        audio.fail = None  # microfone conectado
        self.assertTrue(h.wait(lambda: h.states()[-1] is S.WAITING_WAKEWORD))
        h.stop()

    def test_missing_audio_driver_keeps_retrying_without_traceback(self):  # regressão: refresh() sem PortAudio
        def no_portaudio():
            raise AudioError("Biblioteca de áudio (PortAudio) não encontrada")
        self.addCleanup(setattr, audio_service, "_portaudio", audio_service._portaudio)
        audio_service._portaudio = no_portaudio
        h = Harness(audio=AudioService(CFG), stt=FakeSTT()).start()
        self.assertTrue(h.wait(lambda: h.has(E.AUDIO_ERROR)))
        time.sleep(0.5)
        self.assertTrue(h.thread.is_alive(), "a thread de voz morreu")
        self.assertEqual((h.count(E.AUDIO_ERROR), h.states()[-1]), (1, S.ERROR))
        h.stop()

    def test_lost_signal_is_detected_and_microphone_reopened(self):
        cfg = VoiceConfig(retry_s=0.1, stall_timeout_s=0.3)
        h = Harness(silence(1), stt=FakeSTT(), cfg=cfg).start()
        self.assertTrue(h.wait(lambda: h.has(E.AUDIO_ERROR)))
        self.assertTrue(h.wait(lambda: h.audio.starts >= 2))
        h.stop()

    def test_missing_model_reports_and_closes_microphone(self):
        cfg = VoiceConfig(stt_model_dir=str(Path(tempfile.mkdtemp()) / "nao_existe"))
        h = Harness(audio=FakeAudio(silence(1)), stt=STTService(cfg), cfg=cfg).start()
        self.assertTrue(h.wait(lambda: h.has(E.STT_UNAVAILABLE)))
        h.thread.join(5)
        self.assertIn("não encontrado", h.events[[e for e, _ in h.events].index(E.STT_UNAVAILABLE)][1][0])
        self.assertFalse(h.audio.active)
        self.assertEqual(h.states()[-1], S.ERROR)


class TestSTT(unittest.TestCase):
    def test_missing_model_message(self):
        svc = STTService(VoiceConfig(stt_model_dir=str(Path(tempfile.mkdtemp()) / "x")))
        with self.assertRaisesRegex(ModelNotFoundError, "Modelo de reconhecimento de voz não encontrado"):
            svc.load()

    def test_transcribe_joins_segments_and_filters_noise(self):
        seg = lambda t, ns=0.0, lp=-0.2: SimpleNamespace(text=t, no_speech_prob=ns, avg_logprob=lp)
        calls = []

        class Model:
            def transcribe(self, audio, **kw):
                calls.append((audio.dtype, kw["language"], kw["beam_size"]))
                return iter([seg(" Qual é "), seg("lixo", 0.9, -2.0), seg("o clima?")]), None

        svc = STTService(CFG)
        svc._model = Model()
        self.assertEqual(svc.transcribe(speech(1)), "Qual é o clima?")
        svc.transcribe(speech(1), fast=True)
        self.assertEqual(calls, [(np.dtype("float32"), "pt", 5), (np.dtype("float32"), "pt", 1)])
        self.assertEqual(svc.transcribe(silence(0.1)), "")  # curto demais: nem chama o modelo
        svc._model = SimpleNamespace(transcribe=lambda *a, **k: (iter([seg("Legendas pela comunidade Amara.org")]), None))
        self.assertEqual(svc.transcribe(speech(1)), "")


class FakeSD:  # sounddevice falso: microfone de 48 kHz que recusa 16 kHz
    class PortAudioError(Exception): pass

    def __init__(self, devices=None, start_error=None):
        FakeSD.streams = []
        self.default = SimpleNamespace(device=(0, 1))
        self._devices, self.start_error = devices or [dict(name="Mic Fake", max_input_channels=1, default_samplerate=48000.0),
                                                       dict(name="Alto-falante", max_input_channels=0, default_samplerate=48000.0)], start_error

    def query_devices(self): return self._devices

    def InputStream(self, **kw):
        sd = self
        if kw["samplerate"] == 16000:
            raise FakeSD.PortAudioError("Invalid sample rate")

        class Stream:
            callback = kw["callback"]
            def start(self):
                if sd.start_error: raise FakeSD.PortAudioError(sd.start_error)
            def stop(self): pass
            def close(self): pass
        FakeSD.streams.append(Stream())
        return FakeSD.streams[-1]


class TestAudio(unittest.TestCase):
    def patched(self, **kw):
        fake = FakeSD(**kw)
        fake.PortAudioError = FakeSD.PortAudioError
        self.addCleanup(setattr, audio_service, "_portaudio", audio_service._portaudio)
        audio_service._portaudio = lambda: fake
        return fake

    def test_lists_only_inputs_and_falls_back_to_native_rate_with_resampling(self):
        self.patched()
        self.assertEqual([d.name for d in audio_service.list_input_devices()], ["Mic Fake"])
        a = AudioService(CFG)
        self.assertEqual(a.start(), "Mic Fake")
        for _ in range(4):  # 4 blocos de 30 ms a 48 kHz (o reamostrador tem um pequeno atraso inicial)
            FakeSD.streams[0].callback(np.full((1440, 1), 100, np.int16), 1440, None, None)
        frame = a.read(0.1)
        self.assertEqual((len(frame), frame.dtype), (480, np.int16))
        self.assertTrue(a.resampling and a.native_rate == 48000 and a.device_name == "Mic Fake")
        a.stop()
        self.assertFalse(a.is_active)

    def test_no_input_device(self):
        self.patched(devices=[dict(name="Alto-falante", max_input_channels=0, default_samplerate=48000.0)])
        with self.assertRaisesRegex(MicrophoneUnavailable, "Microfone não encontrado"):
            AudioService(CFG).start()

    def test_selected_device_missing_busy_and_denied(self):
        self.patched()
        with self.assertRaises(MicrophoneUnavailable):
            AudioService(VoiceConfig(device="Webcam")).start()
        self.assertEqual(AudioService(VoiceConfig(device="mic")).start(), "Mic Fake")
        self.patched(start_error="Device unavailable")
        with self.assertRaisesRegex(AudioError, "ocupado"):
            AudioService(CFG).start()
        self.assertIn("Permissão", AudioService._explain(RuntimeError("Access is denied")))

    def test_streaming_resample_keeps_exact_frames_and_duration(self):
        from app.services.audio_service import _Reframer
        for src in (44100, 48000, 16000):
            rf, total, blk = _Reframer(src, 16000, 480), 0, src * 30 // 1000
            for _ in range(100):  # 3 s
                frames = rf.push(np.zeros(blk, np.int16))
                self.assertTrue(all(len(f) == 480 for f in frames))
                total += len(frames) * 480
            self.assertLess(abs(total / 16000 - 3.0), 0.1, src)  # duração preservada (sem acelerar nem alongar)

    def test_resampler_rejects_aliasing_unlike_the_old_one(self):
        from app.services.audio_service import _Reframer
        t = np.arange(48000 * 2) / 48000
        tone = (8000 * np.sin(2 * np.pi * 10000 * t)).astype(np.int16)  # 10 kHz: acima do Nyquist de 16 kHz (8 kHz)
        rf, out = _Reframer(48000, 16000, 480), []
        for i in range(0, len(tone) - 1440 + 1, 1440):
            out += rf.push(tone[i:i + 1440])
        y = np.concatenate(out[10:]).astype(np.float32)
        old = np.concatenate([tone[i:i + 1440].reshape(480, 3).mean(axis=1) for i in range(0, len(tone) - 1439, 1440)])[300:]
        db = lambda x: 20 * np.log10(np.sqrt((x.astype(np.float32) ** 2).mean()) / 8000 + 1e-9)
        self.assertLess(db(y), -60)   # soxr: o tom de 10 kHz some
        self.assertGreater(db(old), -20)  # média simples (método antigo): o tom vira um "fantasma" dentro da banda

    def test_missing_portaudio_is_a_clean_error(self):
        sys.modules.pop("sounddevice", None)
        try:
            list_ok = audio_service.list_input_devices()  # com PortAudio instalado a lista pode ser vazia
            self.assertIsInstance(list_ok, list)
        except AudioError as exc:
            self.assertRegex(str(exc), "PortAudio|sounddevice|dispositivos")


class TestConfig(unittest.TestCase):
    def test_voice_config_defaults_and_overrides(self):
        c = Config(data_dir=Path(tempfile.mkdtemp()))
        self.assertEqual((c.voice_config().sample_rate, c.voice_config().stt_model), (16000, "small"))
        c.set("listening.sensitivity", 0.8)
        c.set("listening.campo_inexistente", 1)
        self.assertEqual(c.voice_config().sensitivity, 0.8)
        self.assertTrue(str(c.voice_config().stt_model_path).endswith("faster-whisper-small"))


class TestUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def build(self, audio, stt):
        from PySide6.QtCore import QObject, Slot
        from app.ui.main_window import MainWindow
        from app.ui.voice_bridge import VoiceBridge

        class Recorder(QObject):
            threads = set()
            @Slot(str)
            def on_text(self, _): self.threads.add(threading.current_thread())

        window = MainWindow(Config(data_dir=Path(tempfile.mkdtemp())))
        voice = VoiceService(CFG, audio=audio, stt=stt)
        VoiceBridge(window, voice)
        rec = Recorder()
        voice.text_recognized.connect(rec.on_text)
        self.addCleanup(voice.shutdown)
        return window, voice, rec

    def wait(self, pred, timeout=10.0):
        from PySide6.QtTest import QTest
        end = time.monotonic() + timeout
        while time.monotonic() < end and not pred():
            QTest.qWait(10)
        return pred()

    def test_voice_text_reaches_ui_without_blocking_it(self):
        from PySide6.QtCore import QTimer
        from app.ui.states import JarvisState as J
        sig = np.concatenate([silence(1.2), speech(1.2), silence(1.2), speech(1.6), silence(1.2)])
        window, voice, rec = self.build(FakeAudio(sig), FakeSTT(["Ei Jarvis", "Qual é a previsão do tempo?"], delay=0.5))
        seen, gaps, last = [], [], [time.monotonic()]
        window.state_changed.connect(seen.append)
        beat = QTimer()
        beat.timeout.connect(lambda: (gaps.append(time.monotonic() - last[0]), last.__setitem__(0, time.monotonic())))
        beat.start(20)
        voice.start()
        self.assertTrue(self.wait(lambda: any(b._text == "Qual é a previsão do tempo?" for b in window.chat._bubbles)))
        beat.stop()
        self.assertIn(J.LISTENING, seen)
        self.assertIn(J.THINKING, seen)
        self.assertEqual(rec.threads, {threading.main_thread()})  # slot executado na thread da UI
        self.assertLess(max(gaps), 0.3, "a UI travou enquanto o STT rodava")
        voice.shutdown()

    def test_app_works_without_microphone(self):
        from app.ui.states import JarvisState as J
        window, voice, _ = self.build(FakeAudio(fail=MicrophoneUnavailable("Microfone não encontrado")), FakeSTT())
        window.show()
        voice.start()
        self.assertTrue(self.wait(lambda: window.state is J.ERROR))
        self.assertIn("Microfone não encontrado", window.status_bar._message.text())
        before = len(window.chat._bubbles)
        window.input_bar.input.setText("ainda posso digitar")
        window.input_bar.send_button.click()
        self.assertEqual(len(window.chat._bubbles), before + 1)
        self.assertTrue(window.isVisible())
        voice.shutdown()


if __name__ == "__main__":
    unittest.main()


class TestSttPipelineQuality(unittest.TestCase):  # Etapa 5, parte I: o que chega ao STT
    def run_capture(self, signal, texts):
        got = []

        class Spy(FakeSTT):
            def transcribe(self, audio, fast=False):
                got.append((len(audio), audio.copy(), fast))
                return super().transcribe(audio, fast)
        h = Harness(signal, stt=Spy(texts)).start()
        h.settle()
        h.stop()
        return h, got

    @staticmethod
    def soft_speech(sec, ramp=0.25):  # início suave (como "A" de "Abra"): 0 -> 100% em `ramp` s
        x = speech(sec).astype(np.float32)
        x[: int(ramp * SR)] *= np.linspace(0, 1, int(ramp * SR)) ** 2
        return x.astype(np.int16)

    def test_onset_is_preserved_and_final_silence_is_trimmed(self):
        sig = np.concatenate([silence(1.2), self.soft_speech(1.2), silence(1.5)])
        h, got = self.run_capture(sig, ["ei jarvis"])
        n, audio, _ = got[0]
        voiced = np.where(np.abs(audio) > 150)[0]  # ruído tem σ=20; fala >> 150
        lead, tail = voiced[0] / SR, (len(audio) - voiced[-1]) / SR
        self.assertGreater(lead, 0.15, f"início da fala cortado (margem {lead:.2f}s)")
        self.assertLess(tail, 0.5, f"silêncio final excessivo ({tail:.2f}s)")  # sem os ~0,8 s de silêncio da VAD

    def test_second_pass_text_is_used_for_the_command(self):
        h, got = self.run_capture(np.concatenate([silence(1.2), speech(2), silence(1.5)]),
                                  ["Ei Jarvis, abre o spot", "Ei Jarvis, abra o Spotify."])
        self.assertEqual(h.texts(), ["abra o Spotify."])
        self.assertEqual([g[2] for g in got], [True, False])  # 1ª rápida (wake), 2ª de qualidade (comando)

    def test_diagnostics_are_off_by_default_and_never_write_audio_unless_asked(self):
        from app.services.voice_debug import VoiceDiagnostics, audio_stats, KEEP_FILES
        d = VoiceDiagnostics(CFG)
        d.record("x", speech(1), "oi", 0.1)
        self.assertEqual(d.last, {})
        tmp = Path(tempfile.mkdtemp())
        (tmp / "jarvis_debug_antigo.wav").write_bytes(b"x")
        on = VoiceDiagnostics(type(CFG)(debug=True, debug_audio_dir=str(tmp)))
        self.assertFalse(list(tmp.glob("*.wav")))  # gravações antigas removidas ao iniciar
        for _ in range(KEEP_FILES + 4):
            on.record("comando", speech(0.5), "texto", 0.2, {"avg_logprob": -0.3}, -70.0)
        self.assertEqual(len(list(tmp.glob("jarvis_debug_*.wav"))), KEEP_FILES)  # só os últimos
        self.assertEqual(on.last["text"], "texto")
        loud = np.full(16000, 32767, np.int16)
        self.assertGreater(audio_stats(loud)["clipped_pct"], 99)

    def test_benchmark_tool_scores_wavs_without_any_text_rewriting(self):
        import importlib.util, wave
        spec = importlib.util.spec_from_file_location("stt_benchmark", Path(__file__).resolve().parents[1] / "scripts" / "stt_benchmark.py")
        bench = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bench)
        self.assertEqual(bench.wer("Abra o Spotify.", "abra o spotify"), 0)
        self.assertAlmostEqual(bench.wer("Abra o Spotify.", "Ah, porous, vai chefar."), 4 / 3)  # exemplo do usuário: sem "correção"
        d = Path(tempfile.mkdtemp())
        for name, rate in (("01_a.wav", 16000), ("02_a.wav", 48000)):
            with wave.open(str(d / name), "wb") as w:
                w.setnchannels(1), w.setsampwidth(2), w.setframerate(rate)
                w.writeframes((speech(1.0 * rate / SR if rate == SR else 1.0)[: rate].tobytes() if rate == SR else np.zeros(rate, np.int16).tobytes()))
        rows = bench.evaluate_wavs(lambda a: "Abra o Spotify." if len(a) else "", d)
        self.assertEqual([(r["resultado"], r["duracao_s"]) for r in rows], [("correto", 1.0), ("parcialmente correto", 1.0)])  # 1 de 3 palavras trocada
        out = d / "r.csv"
        bench.write_csv(rows, str(out))
        self.assertIn("pretendida", out.read_text(encoding="utf-8"))

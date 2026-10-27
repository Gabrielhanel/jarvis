"""Texto -> fala, 100% local (Kokoro/ONNX), com reprodução controlável.

Só converte em voz o texto recebido: não gera respostas, não interpreta, não executa nada.
Política de falas: FIFO limitada (`max_queue`). `speak(..., interrupt=True)` descarta a fala
atual e a fila e fala a nova. `stop()` interrompe tudo.
Cancelamento: parar silencia o áudio na hora e, se o motor tiver `cancel()` (o Kokoro tem: interrompe a
execução do ONNX Runtime em andamento), também aborta a síntese pendente em milissegundos.
Sem `cancel()`, a thread conclui o trecho atual e descarta o resultado.
"""
from __future__ import annotations

import logging
import queue
import re
import threading
from enum import Enum, auto
from typing import Callable

import numpy as np
from PySide6.QtCore import QObject, Signal

from app.core.config import TTSConfig
from app.services.audio_output import AudioOutput, OutputError

log = logging.getLogger("jarvis.tts")


class TTSError(Exception):
    """Falha de síntese ou de carregamento do mecanismo."""


class TTSModelNotFoundError(TTSError):
    """Modelo de voz ausente no disco."""


class TTSState(Enum):
    IDLE = "idle"
    SYNTHESIZING = "synthesizing"
    PLAYING = "playing"
    STOPPING = "stopping"
    ERROR = "error"


class TTSEvent(Enum):
    STATE_CHANGED = auto()
    MODEL_LOADING = auto()
    MODEL_READY = auto()
    UNAVAILABLE = auto()
    SYNTHESIS_STARTED = auto()
    SPEAKING_STARTED = auto()
    SPEAKING_FINISHED = auto()
    SPEAKING_INTERRUPTED = auto()
    TTS_ERROR = auto()


# ---------- tratamento de texto (funções puras) ----------
def normalize_text(text: str) -> str:
    """Limpa o texto sem mudar o conteúdo: remove controles e marcações de markdown (*, `, #, marcadores
    de lista), transforma quebras de linha em pausa e colapsa espaços. Acentos, números e Unicode ficam."""
    if not isinstance(text, str):
        return ""
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", text)
    text = re.sub(r"[*`#~]+", "", text)
    text = re.sub(r"(?m)^\s*[-•]\s+", "", text)
    text = re.sub(r"(?<=[^\s.!?:;,…])\s*\n+\s*", ". ", text)
    return re.sub(r"\s+", " ", text).strip()


def _split_words(text: str, max_chars: int) -> list[str]:
    out, cur = [], ""
    for word in text.split():
        if cur and len(cur) + 1 + len(word) > max_chars:
            out.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    return out + ([cur] if cur else [])


def _split_long(sentence: str, max_chars: int) -> list[str]:
    if len(sentence) <= max_chars:
        return [sentence]
    out, cur = [], ""
    for part in re.split(r"(?<=[,;:])\s+", sentence):  # prefere cortar em vírgula/ponto e vírgula
        for chunk in (_split_words(part, max_chars) if len(part) > max_chars else [part]):
            if cur and len(cur) + 1 + len(chunk) > max_chars:
                out.append(cur)
                cur = chunk
            else:
                cur = f"{cur} {chunk}".strip()
    return out + ([cur] if cur else [])


def split_text(text: str, max_chars: int) -> list[str]:
    """Divide em frases (mantendo a ordem). Frases longas viram trechos cortados em vírgulas ou espaços,
    nunca no meio de palavras; fragmentos minúsculos ("Sr.", "Sim.") se juntam à frase seguinte."""
    pieces = [p for s in re.split(r"(?<=[.!?…])\s+", text) for p in _split_long(s, max_chars)]
    segments: list[str] = []
    for piece in pieces:
        if segments and len(segments[-1]) < 16 and len(segments[-1]) + 1 + len(piece) <= max_chars:
            segments[-1] += " " + piece
        else:
            segments.append(piece)
    return [s for s in segments if s]


def _cut(text: str, limit: int) -> str:
    head = text[:limit]
    end = max(head.rfind(c) for c in ".!?…")
    return head[: end + 1] if end > limit // 2 else head.rsplit(" ", 1)[0]


# ---------- mecanismo ----------
def _onnx_errors() -> tuple[type[BaseException], ...]:
    """Exceções do ONNX Runtime (não herdam de RuntimeError): modelo corrompido, incompatível etc."""
    try:
        from onnxruntime.capi import onnxruntime_pybind11_state as ort
    except ImportError:
        return ()
    names = ("Fail", "InvalidArgument", "NoSuchFile", "NoModel", "EngineError", "RuntimeException", "InvalidProtobuf",
             "ModelLoaded", "NotImplemented", "InvalidGraph", "EPFail")
    return tuple(getattr(ort, n) for n in names if hasattr(ort, n))


class KokoroEngine:
    """Kokoro-82M via ONNX Runtime (CPU). Interface do mecanismo: load(), is_loaded, synthesize(), set_rate()."""

    def __init__(self, cfg: TTSConfig) -> None:
        self._cfg = cfg
        self._kokoro = None
        self._rate = cfg.rate
        self._run_options = None  # RunOptions compartilhado: terminate=True aborta a inferência em andamento
        self._lock = threading.Lock()

    @property
    def is_loaded(self) -> bool:
        return self._kokoro is not None

    def set_rate(self, rate: float) -> None:
        self._rate = float(np.clip(rate, 0.5, 2.0))

    def cancel(self) -> None:
        """Aborta a síntese em andamento (a próxima chamada a synthesize() volta ao normal)."""
        if self._run_options is not None:
            self._run_options.terminate = True

    def load(self) -> None:
        with self._lock:
            if self._kokoro is not None:
                return
            for path in (self._cfg.model_file, self._cfg.voices_file):
                if not path.is_file():
                    raise TTSModelNotFoundError(
                        f"Modelo de voz (TTS) não encontrado: {path}. Baixe com: python scripts/download_tts_model.py")
            try:
                from kokoro_onnx import Kokoro
            except ImportError as exc:
                raise TTSError("Pacote 'kokoro-onnx' não instalado. Rode: pip install -r requirements.txt") from exc
            try:
                kokoro = Kokoro(str(self._cfg.model_file), str(self._cfg.voices_file))
            except (RuntimeError, OSError, ValueError, KeyError, EOFError, *_onnx_errors()) as exc:
                raise TTSError(f"Falha ao carregar o modelo de voz (arquivo inválido ou incompatível): {exc}") from exc
            if self._cfg.voice not in kokoro.get_voices():
                pt = [v for v in kokoro.get_voices() if v[:2] in ("pf", "pm")]
                raise TTSError(f"Voz '{self._cfg.voice}' não existe neste modelo. Vozes em português: {', '.join(pt)}")
            import onnxruntime as ort
            self._run_options = ort.RunOptions()
            run = kokoro.sess.run  # o Kokoro chama sess.run(None, inputs); injetamos o RunOptions cancelável
            kokoro.sess.run = lambda names, feed, run_options=None: run(names, feed, run_options or self._run_options)
            self._kokoro = kokoro
            log.info("[TTS] Modelo carregado (voz %s)", self._cfg.voice)

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        if self._kokoro is None:
            raise TTSError("Mecanismo de voz não carregado")
        try:
            with self._lock:
                self._run_options.terminate = False
                samples, rate = self._kokoro.create(text, voice=self._cfg.voice, speed=self._rate,
                                                    lang=self._cfg.lang, trim=True)
        except (RuntimeError, ValueError, IndexError, *_onnx_errors()) as exc:
            raise TTSError(f"Falha na síntese: {exc}") from exc
        return samples.astype(np.float32, copy=False), int(rate)


# ---------- pipeline (Python puro, testável sem Qt) ----------
class TTSPipeline:
    def __init__(self, cfg: TTSConfig, engine, output: AudioOutput, emit: Callable[..., None]) -> None:
        self.cfg, self.engine, self.output, self._emit = cfg, engine, output, emit
        self.state = TTSState.IDLE
        self._requests: queue.Queue[tuple[int, str]] = queue.Queue(maxsize=cfg.max_queue)
        self._lock = threading.Lock()
        self._gen = 0  # cada stop() invalida as falas pedidas antes dele
        self._shutdown = threading.Event()

    # --- API (segura entre threads) ---
    def speak(self, text: str, interrupt: bool = False) -> bool:
        cleaned = normalize_text(text)
        if not cleaned:
            log.debug("[TTS] Texto vazio ignorado")
            return False
        if len(cleaned) > self.cfg.max_text_chars:
            cleaned = _cut(cleaned, self.cfg.max_text_chars)
            log.warning("[TTS] Texto muito longo: cortado em %d caracteres", len(cleaned))
        if interrupt:
            self.stop()
        with self._lock:
            gen = self._gen
        try:
            self._requests.put_nowait((gen, cleaned))
        except queue.Full:
            log.warning("[TTS] Fila cheia: fala descartada")
            return False
        return True

    def stop(self) -> None:
        with self._lock:
            self._gen += 1
        while True:
            try:
                self._requests.get_nowait()
            except queue.Empty:
                break
        self.output.abort()  # silêncio imediato
        cancel = getattr(self.engine, "cancel", None)
        if cancel is not None:
            cancel()  # aborta a síntese pendente, quando o motor permite
        if self._set_state(TTSState.STOPPING, only_if=(TTSState.SYNTHESIZING, TTSState.PLAYING)):
            log.info("[TTS] Interrupção solicitada")

    def shutdown(self) -> None:
        self._shutdown.set()
        self.stop()

    def is_speaking(self) -> bool:
        return self.state in (TTSState.SYNTHESIZING, TTSState.PLAYING)

    def set_rate(self, rate: float) -> None:
        self.engine.set_rate(rate)

    def set_volume(self, volume: float) -> None:
        self.output.set_volume(volume)

    # --- thread de trabalho ---
    def run(self) -> None:
        self._ensure_engine()
        while not self._shutdown.is_set():
            try:
                gen, text = self._requests.get(timeout=0.2)
            except queue.Empty:
                continue
            self._process(gen, text)
        self.output.abort()
        self.output.close()
        self._set_state(TTSState.IDLE)

    def _ensure_engine(self) -> bool:
        if self.engine.is_loaded:
            return True
        self._emit(TTSEvent.MODEL_LOADING)
        log.info("[TTS] Inicializando mecanismo")
        try:
            self.engine.load()
        except TTSError as exc:  # inclui modelo ausente; novas tentativas ocorrem a cada fala
            log.error("[TTS] %s", exc)
            self._emit(TTSEvent.UNAVAILABLE, str(exc))
            return False
        self._emit(TTSEvent.MODEL_READY)
        return True

    def _cancelled(self, gen: int) -> bool:
        return gen != self._gen or self._shutdown.is_set()

    def _process(self, gen: int, text: str) -> None:
        if self._cancelled(gen) or not self._ensure_engine():
            return
        segments = split_text(text, self.cfg.max_segment_chars)
        self._set_state(TTSState.SYNTHESIZING)
        log.info("[TTS] Síntese iniciada (%d trecho(s))", len(segments))
        self._emit(TTSEvent.SYNTHESIS_STARTED)
        playing, error, pending = False, None, []
        try:
            for segment in segments:
                if self._cancelled(gen):
                    break
                audio, rate = self.engine.synthesize(segment)
                if self._cancelled(gen):
                    break
                audio = np.concatenate([audio, np.zeros(int(rate * self.cfg.pause_ms / 1000), np.float32)])
                if self.cfg.stream_playback:
                    playing = self._play(audio, rate, playing)
                else:
                    pending.append((audio, rate))
            if pending and not self._cancelled(gen):
                playing = self._play(np.concatenate([a for a, _ in pending]), pending[0][1], playing)
            while playing and not self._cancelled(gen) and not self.output.wait_drained(0.1):
                pass
        except TTSError as exc:
            if not self._cancelled(gen):  # erro causado por cancelamento não é falha
                error = str(exc)
                log.error("[TTS] Erro de síntese: %s", exc)
        except OutputError as exc:
            error = str(exc)
            log.error("[TTS] Erro de reprodução: %s", exc)
        finally:
            self.output.close()
            if error:
                self._set_state(TTSState.ERROR)
                self._emit(TTSEvent.TTS_ERROR, error)
            elif self._cancelled(gen):
                log.info("[TTS] Reprodução interrompida")
                self._emit(TTSEvent.SPEAKING_INTERRUPTED)
            else:
                log.info("[TTS] Reprodução concluída")
                self._emit(TTSEvent.SPEAKING_FINISHED)
            self._set_state(TTSState.IDLE)

    def _play(self, audio: np.ndarray, rate: int, playing: bool) -> bool:
        if not playing:
            self._emit(TTSEvent.SPEAKING_STARTED)  # antes de abrir o dispositivo: o microfone é suspenso primeiro
            self._set_state(TTSState.PLAYING)
            log.info("[TTS] Reprodução iniciada")
            self.output.open(rate)
        self.output.push(audio, rate)
        return True

    def _set_state(self, state: TTSState, only_if: tuple[TTSState, ...] | None = None) -> bool:
        with self._lock:
            if state is self.state or (only_if is not None and self.state not in only_if):
                return False
            self.state = state
        self._emit(TTSEvent.STATE_CHANGED, state)
        return True


class TTSService(QObject):
    """Roda o TTSPipeline numa thread própria e expõe os eventos como sinais Qt."""

    state_changed = Signal(object)  # TTSState
    model_loading = Signal()
    model_ready = Signal()
    unavailable = Signal(str)
    synthesis_started = Signal()
    speaking_started = Signal()
    speaking_finished = Signal()
    speaking_interrupted = Signal()
    tts_error = Signal(str)

    def __init__(self, cfg: TTSConfig, *, engine=None, output=None, parent=None) -> None:
        super().__init__(parent)
        self.config = cfg
        self._pipeline = TTSPipeline(cfg, engine or KokoroEngine(cfg), output or AudioOutput(cfg.output_device, cfg.volume),
                                     self._dispatch)
        self._thread: threading.Thread | None = None
        E = TTSEvent
        self._routes = {E.STATE_CHANGED: self.state_changed, E.MODEL_LOADING: self.model_loading, E.MODEL_READY: self.model_ready,
                        E.UNAVAILABLE: self.unavailable, E.SYNTHESIS_STARTED: self.synthesis_started,
                        E.SPEAKING_STARTED: self.speaking_started, E.SPEAKING_FINISHED: self.speaking_finished,
                        E.SPEAKING_INTERRUPTED: self.speaking_interrupted, E.TTS_ERROR: self.tts_error}

    def _dispatch(self, event: TTSEvent, *payload) -> None:
        self._routes[event].emit(*payload)

    @property
    def state(self) -> TTSState:
        return self._pipeline.state

    def is_speaking(self) -> bool:
        return self._pipeline.is_speaking()

    def start(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._pipeline.run, name="jarvis-tts", daemon=True)
            self._thread.start()

    def speak(self, text: str, interrupt: bool = False) -> bool:
        return self._pipeline.speak(text, interrupt)

    def stop(self) -> None:
        self._pipeline.stop()

    def set_volume(self, volume: float) -> None:
        self._pipeline.set_volume(volume)

    def set_rate(self, rate: float) -> None:
        self._pipeline.set_rate(rate)

    def shutdown(self, timeout: float = 2.0) -> None:
        """Silencia na hora e espera até `timeout` s. Uma síntese em andamento não é cancelável: se passar do
        prazo ela é abandonada (a thread é daemon e morre com o processo; nada fica órfão)."""
        self._pipeline.shutdown()
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                log.warning("[TTS] Síntese em andamento abandonada no encerramento")

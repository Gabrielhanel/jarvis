"""Entrada de voz: microfone -> VAD -> wake word -> STT -> texto.

`VoicePipeline` é a máquina de estados (Python puro, testável sem Qt). `VoiceService` roda o
pipeline numa QThread e traduz os eventos em sinais Qt. O texto reconhecido é só dado: nada
aqui interpreta, executa ou envia o texto a uma IA.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from collections import deque
from enum import Enum, auto
from typing import Callable

import numpy as np
from PySide6.QtCore import QThread, Signal

from app.core.config import VoiceConfig
from app.services.audio_service import AudioError, AudioService, MicrophoneUnavailable
from app.services.stt_service import STTError, STTService
from app.services.vad_service import VADService, VadEvent
from app.services.voice_debug import VoiceDiagnostics
from app.services.wakeword_service import WakeWordService

log = logging.getLogger("jarvis.voice")


class VoiceState(Enum):
    IDLE = "idle"
    WAITING_WAKEWORD = "waiting_wakeword"
    LISTENING_COMMAND = "listening_command"
    TRANSCRIBING = "transcribing"
    ERROR = "error"


class VoiceEvent(Enum):
    STATE_CHANGED = auto()
    MICROPHONE_STARTED = auto()
    MICROPHONE_STOPPED = auto()
    MICROPHONE_UNAVAILABLE = auto()
    AUDIO_ERROR = auto()
    MODEL_LOADING = auto()
    MODEL_READY = auto()
    STT_UNAVAILABLE = auto()
    WAKE_WORD_DETECTED = auto()
    SPEECH_STARTED = auto()
    SPEECH_FINISHED = auto()
    TRANSCRIPTION_STARTED = auto()
    TRANSCRIPTION_FINISHED = auto()
    TRANSCRIPTION_ERROR = auto()
    TEXT_RECOGNIZED = auto()
    COMMAND_TIMEOUT = auto()


_STATE_LOG = {VoiceState.IDLE: "Voz parada", VoiceState.WAITING_WAKEWORD: "Aguardando wake word",
              VoiceState.LISTENING_COMMAND: "Capturando comando", VoiceState.TRANSCRIBING: "Transcrição iniciada",
              VoiceState.ERROR: "Estado de erro"}


class VoicePipeline:
    def __init__(self, cfg: VoiceConfig, audio, vad: VADService, stt, wake: WakeWordService,
                 emit: Callable[..., None], diag: VoiceDiagnostics | None = None) -> None:
        self.cfg, self.audio, self.vad, self.stt, self.wake, self._emit = cfg, audio, vad, stt, wake, emit
        self.diag = diag or VoiceDiagnostics(cfg)  # no-op quando cfg.debug é False
        self.state = VoiceState.IDLE
        self._stop = threading.Event()
        self._commands: queue.Queue[str] = queue.Queue()
        self._preroll: deque[np.ndarray] = deque(maxlen=cfg.preroll_frames + cfg.start_frames + 1)
        self._segment: list[np.ndarray] | None = None
        self._deadline = 0.0
        self._last_frame = time.monotonic()
        self._mic_on = False
        self._last_error = ""
        self._paused, self._paused_at, self._hold_until = False, 0.0, 0.0  # captura suspensa enquanto o Jarvis fala

    # --- controle (seguro entre threads) ---
    def stop(self) -> None:
        self._stop.set()

    def trigger_manual(self) -> None:
        self._commands.put("manual")

    def suspend(self) -> None:
        """Ignora o áudio (o microfone continua aberto). Usado enquanto o TTS fala."""
        self._commands.put("suspend")

    def resume(self, holdoff_ms: int = 0) -> None:
        """Volta a ouvir depois de `holdoff_ms` (deixa o eco da fala se dissipar)."""
        self._commands.put(f"resume:{int(holdoff_ms)}")

    # --- ciclo de vida ---
    def run(self) -> None:
        try:
            if self._open_microphone() and self._load_model():
                self._set_state(VoiceState.WAITING_WAKEWORD)
                self._loop()
        except Exception:  # noqa: BLE001 - registrado com traceback e convertido em estado seguro
            log.exception("[VOICE] Erro inesperado no pipeline de voz")
            self._emit(VoiceEvent.AUDIO_ERROR, "Erro interno no sistema de voz (veja o log).")
            self._set_state(VoiceState.ERROR)
        finally:
            self.audio.stop()
            if self._mic_on:
                self._mic_on = False
                self._emit(VoiceEvent.MICROPHONE_STOPPED)
            if self._stop.is_set() or self.state is not VoiceState.ERROR:
                self._set_state(VoiceState.IDLE)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._handle_commands()
            frame = self.audio.read(0.25)
            self._check_timeout()
            if frame is None:
                if time.monotonic() - self._last_frame > self.cfg.stall_timeout_s:
                    self._recover_microphone()
                continue
            self._last_frame = time.monotonic()
            if self._paused:
                continue
            if self._hold_until:
                if time.monotonic() < self._hold_until:
                    continue
                self._hold_until = 0.0
                self._preroll.clear()
                self.vad.reset()
            self._on_frame(frame)

    def _open_microphone(self) -> bool:
        while not self._stop.is_set():
            try:
                name = self.audio.start()
            except MicrophoneUnavailable as exc:
                self._report(VoiceEvent.MICROPHONE_UNAVAILABLE, str(exc))
            except AudioError as exc:
                self._report(VoiceEvent.AUDIO_ERROR, str(exc))
            else:
                self._mic_on, self._last_error, self._last_frame = True, "", time.monotonic()
                self._emit(VoiceEvent.MICROPHONE_STARTED, name)
                return True
            self._set_state(VoiceState.ERROR)
            if self._stop.wait(self.cfg.retry_s):
                break
            self.audio.refresh()  # o microfone pode ter sido conectado depois
        return False

    def _load_model(self) -> bool:
        self._emit(VoiceEvent.MODEL_LOADING)
        log.info("[VOICE] Carregando modelo de voz")
        try:
            self.stt.load()
        except STTError as exc:
            log.error("[VOICE] %s", exc)
            self._emit(VoiceEvent.STT_UNAVAILABLE, str(exc))
            self._set_state(VoiceState.ERROR)
            return False
        self._emit(VoiceEvent.MODEL_READY)
        return True

    def _recover_microphone(self) -> None:
        self._report(VoiceEvent.AUDIO_ERROR, "Sem sinal do microfone (desconectado ou ocupado)")
        self._set_state(VoiceState.ERROR)
        self.audio.stop()
        self._mic_on, self._segment = False, None
        self._preroll.clear()
        self._emit(VoiceEvent.MICROPHONE_STOPPED)
        if self._open_microphone():
            self.vad.reset()
            self._set_state(VoiceState.WAITING_WAKEWORD)

    # --- processamento ---
    def _on_frame(self, frame: np.ndarray) -> None:
        result = self.vad.process(frame)
        (self._segment if self._segment is not None else self._preroll).append(frame)
        if result.event is VadEvent.SPEECH_START and self._segment is None:
            self._segment = list(self._preroll)  # inclui o pré-roll: o começo da fala não é cortado
            self._preroll.clear()
            if self.state is VoiceState.LISTENING_COMMAND:
                log.info("[VOICE] Fala iniciada")
                self._emit(VoiceEvent.SPEECH_STARTED)
        elif self._segment is not None:
            limit = self.cfg.max_utterance_s if self.state is VoiceState.LISTENING_COMMAND else self.cfg.wake_window_s
            too_long = len(self._segment) * self.cfg.frame_ms >= limit * 1000
            if result.event is VadEvent.SPEECH_END or too_long:
                segment, self._segment = self._segment, None
                if result.event is VadEvent.SPEECH_END:  # não manda ao STT o silêncio de fim de fala (mantém um respiro)
                    cut = max(0, self.cfg.end_frames - self.cfg.tail_keep_frames)
                    segment = segment[:-cut] if 0 < cut < len(segment) else segment
                audio = np.concatenate(segment)
                if too_long:
                    self.vad.reset()
                self._on_segment(audio)

    def _check_timeout(self) -> None:
        if (self.state is VoiceState.LISTENING_COMMAND and self._segment is None and not self._paused
                and time.monotonic() > self._deadline):
            log.info("[VOICE] Timeout: nenhum comando falado")
            self._emit(VoiceEvent.COMMAND_TIMEOUT)
            self._back_to_waiting()

    def _on_segment(self, audio: np.ndarray) -> None:
        if self.state is VoiceState.WAITING_WAKEWORD:
            self._process_wake(audio)
        elif self.state is VoiceState.LISTENING_COMMAND:
            self._process_command(audio)

    def _process_wake(self, audio: np.ndarray) -> None:
        t0 = time.monotonic()
        try:
            result = self.wake.check_audio(audio)
        except STTError as exc:
            self._report(VoiceEvent.TRANSCRIPTION_ERROR, str(exc))
            return
        self.diag.record("wake", audio, getattr(self.wake, "last_text", ""), time.monotonic() - t0,
                         getattr(self.stt, "last_info", None), self.vad.noise_floor_db, self.audio,
                         "-> ATIVOU" if result.found else "-> ignorada")
        if not result.found:
            log.debug("[VOICE] Fala ignorada: sem wake word")
            return
        log.info("[VOICE] Wake word detectada")
        self._emit(VoiceEvent.WAKE_WORD_DETECTED)
        self._enter_command()
        if result.remainder:  # "Ei Jarvis, abra o Spotify" numa frase só
            # A checagem da wake word usa decodificação rápida (beam 1). O comando merece a de qualidade total:
            # refaz a transcrição do mesmo áudio e usa o resto da frase desta segunda passada, se ela confirmar a wake word.
            try:
                better = self.wake.match(self._transcribe(audio, fast=False, kind="comando(2ª passada)"))
            except STTError as exc:
                log.warning("[VOICE] Segunda passada falhou, mantendo a primeira: %s", exc)
                better = None
            self._deliver(better.remainder if better is not None and better.found and better.remainder else result.remainder)

    def _process_command(self, audio: np.ndarray) -> None:
        log.info("[VOICE] Fala encerrada")
        self._emit(VoiceEvent.SPEECH_FINISHED)
        self._set_state(VoiceState.TRANSCRIBING)
        self._emit(VoiceEvent.TRANSCRIPTION_STARTED)
        try:
            text = self._transcribe(audio, fast=False, kind="comando")
        except STTError as exc:
            log.error("[VOICE] %s", exc)
            self._emit(VoiceEvent.TRANSCRIPTION_ERROR, str(exc))
            self._back_to_waiting()
            return
        self._emit(VoiceEvent.TRANSCRIPTION_FINISHED, text)
        if text:
            self._deliver(text)
        else:
            log.info("[VOICE] Nenhuma fala reconhecida")
            self._back_to_waiting()

    def _transcribe(self, audio: np.ndarray, fast: bool, kind: str) -> str:
        t0 = time.monotonic()
        text = self.stt.transcribe(audio, fast=fast)
        self.diag.record(kind, audio, text, time.monotonic() - t0, getattr(self.stt, "last_info", None),
                         self.vad.noise_floor_db, self.audio)
        return text

    def _deliver(self, text: str) -> None:
        log.info("[VOICE] Texto reconhecido: %s", text)
        self._emit(VoiceEvent.TEXT_RECOGNIZED, text, uuid.uuid4().hex)  # ID da submissão: nasce na fala finalizada
        self._back_to_waiting()

    def _enter_command(self) -> None:
        self._segment = None
        self.vad.reset()
        self._deadline = time.monotonic() + self.cfg.command_timeout_s
        self._set_state(VoiceState.LISTENING_COMMAND)

    def _back_to_waiting(self) -> None:
        if self.state in (VoiceState.TRANSCRIBING, VoiceState.LISTENING_COMMAND):
            log.info("[VOICE] Voltando para modo de espera")
        self._segment = None
        self._set_state(VoiceState.WAITING_WAKEWORD)

    def _handle_commands(self) -> None:
        while True:
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                return
            if command == "suspend":
                self._pause()
            elif command.startswith("resume:"):
                self._unpause(int(command[7:]))
            elif command == "manual" and self.state is VoiceState.WAITING_WAKEWORD:
                log.info("[VOICE] Escuta manual (teste)")
                self._enter_command()
            elif command == "manual" and self.state is VoiceState.LISTENING_COMMAND and self._segment is None:
                log.info("[VOICE] Escuta manual cancelada")
                self._back_to_waiting()

    def _pause(self) -> None:
        if not self._paused:
            self._paused, self._paused_at, self._hold_until = True, time.monotonic(), 0.0
            self._segment = None
            self._preroll.clear()
            self.vad.reset()
            log.info("[VOICE] Captura suspensa (o Jarvis está falando)")

    def _unpause(self, holdoff_ms: int) -> None:
        if self._paused:
            now = time.monotonic()
            self._paused, self._hold_until = False, now + holdoff_ms / 1000
            self._deadline += now - self._paused_at + holdoff_ms / 1000  # a espera pelo comando não corre durante a fala
            log.info("[VOICE] Captura retomada")

    def _set_state(self, state: VoiceState) -> None:
        if state is not self.state:
            self.state = state
            log.info("[VOICE] %s", _STATE_LOG[state])
            self._emit(VoiceEvent.STATE_CHANGED, state)

    def _report(self, event: VoiceEvent, message: str) -> None:
        if message != self._last_error:  # não repete o mesmo erro a cada tentativa
            self._last_error = message
            log.warning("[VOICE] %s", message)
            self._emit(event, message)


class VoiceService(QThread):
    """Roda o VoicePipeline fora da thread da UI e expõe os eventos como sinais."""

    state_changed = Signal(object)  # VoiceState
    microphone_started = Signal(str)
    microphone_stopped = Signal()
    microphone_unavailable = Signal(str)
    audio_error = Signal(str)
    model_loading = Signal()
    model_ready = Signal()
    stt_unavailable = Signal(str)
    wake_word_detected = Signal()
    speech_started = Signal()
    speech_finished = Signal()
    transcription_started = Signal()
    transcription_finished = Signal(str)
    transcription_error = Signal(str)
    text_recognized = Signal(str, str)  # (texto final, id da submissão)
    command_timeout = Signal()

    def __init__(self, cfg: VoiceConfig, *, audio=None, stt=None, parent=None) -> None:
        super().__init__(parent)
        self.config = cfg
        self._audio, self._stt = audio, stt  # injeção: testes e futuras trocas de mecanismo
        self._pipeline: VoicePipeline | None = None
        self._suspended = False
        E = VoiceEvent
        self._routes = {E.STATE_CHANGED: self.state_changed, E.MICROPHONE_STARTED: self.microphone_started,
                        E.MICROPHONE_STOPPED: self.microphone_stopped, E.MICROPHONE_UNAVAILABLE: self.microphone_unavailable,
                        E.AUDIO_ERROR: self.audio_error, E.MODEL_LOADING: self.model_loading, E.MODEL_READY: self.model_ready,
                        E.STT_UNAVAILABLE: self.stt_unavailable, E.WAKE_WORD_DETECTED: self.wake_word_detected,
                        E.SPEECH_STARTED: self.speech_started, E.SPEECH_FINISHED: self.speech_finished,
                        E.TRANSCRIPTION_STARTED: self.transcription_started, E.TRANSCRIPTION_FINISHED: self.transcription_finished,
                        E.TRANSCRIPTION_ERROR: self.transcription_error, E.TEXT_RECOGNIZED: self.text_recognized,
                        E.COMMAND_TIMEOUT: self.command_timeout}

    def run(self) -> None:  # executa na thread de voz; o modelo carrega aqui, sem travar a UI
        stt = self._stt or STTService(self.config)
        wake = WakeWordService(lambda audio: stt.transcribe(audio, fast=True), self.config)
        self._pipeline = VoicePipeline(self.config, self._audio or AudioService(self.config), VADService(self.config),
                                       stt, wake, self._dispatch)
        if self._suspended:
            self._pipeline.suspend()
        self._pipeline.run()

    def _dispatch(self, event: VoiceEvent, *payload) -> None:
        self._routes[event].emit(*payload)

    @property
    def current_state(self) -> VoiceState:
        return self._pipeline.state if self._pipeline is not None else VoiceState.IDLE

    def suspend(self) -> None:  # seguro para chamar de qualquer thread
        self._suspended = True
        if self._pipeline is not None:
            self._pipeline.suspend()

    def resume(self, holdoff_ms: int = 0) -> None:
        self._suspended = False
        if self._pipeline is not None:
            self._pipeline.resume(holdoff_ms)

    def trigger_manual(self) -> None:
        """Modo de teste: escuta um comando sem exigir a wake word (botão Microfone)."""
        if self._pipeline is not None and self.isRunning():
            self._pipeline.trigger_manual()

    def shutdown(self, timeout_ms: int = 5000) -> None:
        if self._pipeline is not None:
            self._pipeline.stop()
        if self.isRunning() and not self.wait(timeout_ms):
            log.warning("[VOICE] A thread de voz não encerrou a tempo")

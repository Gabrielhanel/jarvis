"""Saída de áudio em streaming (sounddevice/PortAudio): empilha amostras, toca, para na hora."""
from __future__ import annotations

import logging
import threading
from collections import deque

import numpy as np

from app.services import audio_service

log = logging.getLogger("jarvis.tts")


class OutputError(Exception):
    """Falha ao abrir ou usar o dispositivo de saída."""


class AudioOutput:
    """Um stream mono por fala: `open()` -> `push()`... -> `wait_drained()` -> `close()`.
    `abort()` silencia em até um bloco de áudio (~20 ms) a partir de qualquer thread."""

    def __init__(self, device: int | str | None = None, volume: float = 1.0) -> None:
        self._device = device
        self._volume = float(np.clip(volume, 0.0, 1.0))
        self._lock = threading.Lock()
        self._chunks: deque[np.ndarray] = deque()
        self._buffered = 0
        self._stream = None
        self._rate = 0
        self._latency = 0.0
        self._abort = threading.Event()
        self._drained = threading.Event()

    @property
    def is_open(self) -> bool:
        return self._stream is not None

    def set_volume(self, volume: float) -> None:
        self._volume = float(np.clip(volume, 0.0, 1.0))

    def open(self, sample_rate: int) -> None:
        if self._stream is not None:
            return
        sd = audio_service._portaudio()
        try:
            outputs = [d for d in sd.query_devices() if d["max_output_channels"] > 0]
        except (sd.PortAudioError, ValueError) as exc:
            raise OutputError(f"Não foi possível listar as saídas de áudio: {exc}") from exc
        if not outputs:
            raise OutputError("Nenhuma saída de áudio disponível")
        native = int(outputs[0]["default_samplerate"])
        last: Exception | None = None
        self._abort.clear()
        with self._lock:
            self._chunks.clear()
            self._buffered = 0
        self._drained.set()
        for rate, channels in dict.fromkeys(((sample_rate, 1), (sample_rate, 2), (native, 1), (native, 2))):
            stream = None
            try:
                stream = sd.OutputStream(device=self._device, samplerate=rate, channels=channels, dtype="float32",
                                         callback=self._callback)
                stream.start()
            except (sd.PortAudioError, ValueError) as exc:
                last = exc
                if stream is not None:
                    stream.close()
                continue
            self._stream, self._rate, self._latency = stream, rate, float(stream.latency)
            return
        raise OutputError(f"Não foi possível abrir a saída de áudio: {last}")

    def push(self, samples: np.ndarray, sample_rate: int) -> None:
        if self._stream is None or self._abort.is_set() or len(samples) == 0:
            return
        data = samples.astype(np.float32, copy=False)
        if sample_rate != self._rate:
            data = np.interp(np.linspace(0, len(data) - 1, int(len(data) * self._rate / sample_rate)),
                             np.arange(len(data)), data).astype(np.float32)
        with self._lock:
            self._chunks.append(data)
            self._buffered += len(data)
            self._drained.clear()

    def wait_drained(self, timeout: float = 0.1) -> bool:
        """True quando tudo foi tocado (inclui a latência do dispositivo); False se esgotou o tempo ou abortou."""
        if not self._drained.wait(timeout):
            return False
        return not self._abort.wait(self._latency)

    def abort(self) -> None:
        self._abort.set()
        with self._lock:
            self._chunks.clear()
            self._buffered = 0

    def close(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            sd = audio_service._portaudio()
            try:
                stream.stop()
                stream.close()
            except sd.PortAudioError as exc:
                log.warning("[TTS] Erro ao fechar a saída de áudio: %s", exc)

    def _callback(self, outdata, frames, time_info, status) -> None:  # thread do PortAudio
        out = np.zeros(frames, np.float32)
        pos = 0
        with self._lock:
            while pos < frames and self._chunks:
                chunk = self._chunks[0]
                take = min(len(chunk), frames - pos)
                out[pos:pos + take] = chunk[:take]
                pos += take
                if take == len(chunk):
                    self._chunks.popleft()
                else:
                    self._chunks[0] = chunk[take:]
            self._buffered -= pos
            if not self._chunks:
                self._drained.set()
        outdata[:] = (np.clip(out * self._volume, -1.0, 1.0)).reshape(-1, 1)

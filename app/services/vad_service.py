"""VAD (há fala ou não?) por energia na banda da voz (200-3400 Hz), com piso de ruído adaptativo."""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum, auto

import numpy as np

from app.core.config import VoiceConfig


class VadEvent(Enum):
    NONE = auto()
    SPEECH_START = auto()
    SPEECH_END = auto()


@dataclass(frozen=True)
class VadResult:
    voice: bool  # este frame parece voz
    event: VadEvent
    level_db: float


class VADService:
    """Um frame vira "voz" se a energia em 200-3400 Hz passar do piso de ruído + margem e se
    a maior parte da energia estiver nessa banda (rejeita hum de ventilador e cliques).
    A fala só começa após `min_speech_ms` de voz e só termina após `silence_ms` sem voz."""

    CALIBRATION_FRAMES = 10  # ~300 ms iniciais para medir o ruído de fundo
    NOISE_FALL, NOISE_RISE, NOISE_RISE_IN_SPEECH = 0.2, 0.02, 0.002

    def __init__(self, cfg: VoiceConfig) -> None:
        n = cfg.frame_samples
        self._cfg = cfg
        self._window = np.hanning(n).astype(np.float32)
        freqs = np.fft.rfftfreq(n, 1 / cfg.sample_rate)
        self._band = (freqs >= 200) & (freqs <= 3400)
        self._scale = n * n * 0.375 * 32768.0**2
        self._noise_db, self._calib, self._calibrated = -70.0, [], False
        self.reset()

    @property
    def noise_floor_db(self) -> float:
        return self._noise_db

    def reset(self, keep_noise: bool = True) -> None:
        if not keep_noise:
            self._noise_db, self._calib, self._calibrated = -70.0, [], False
        self._speaking, self._run, self._silence = False, 0, 0

    def process(self, frame: np.ndarray) -> VadResult:
        cfg = self._cfg
        spec = np.abs(np.fft.rfft(frame.astype(np.float32) * self._window)) ** 2
        band = float(spec[self._band].sum())
        db = 10 * math.log10(band / self._scale + 1e-12)
        if not self._calibrated:
            self._calib.append(db)
            if len(self._calib) >= self.CALIBRATION_FRAMES:
                self._noise_db, self._calibrated = float(np.median(self._calib)), True
            return VadResult(False, VadEvent.NONE, db)

        margin = 6 + (1 - cfg.sensitivity) * 12  # dB acima do ruído
        voice = (db > max(self._noise_db + margin, cfg.min_level_db)
                 and band / (float(spec.sum()) + 1e-9) >= cfg.speech_band_ratio)
        if not voice and not self._speaking:
            alpha = self.NOISE_FALL if db < self._noise_db else self.NOISE_RISE
        else:  # ruído constante e alto acaba sendo absorvido, a fala (curta) não
            alpha = self.NOISE_RISE_IN_SPEECH if db > self._noise_db else 0.0
        self._noise_db += alpha * (db - self._noise_db)

        event = VadEvent.NONE
        if voice:
            self._run, self._silence = self._run + 1, 0
        else:
            self._run, self._silence = max(0, self._run - 1), self._silence + 1
        if not self._speaking and self._run >= cfg.start_frames:
            self._speaking, event = True, VadEvent.SPEECH_START
        elif self._speaking and self._silence >= cfg.end_frames:
            self._speaking, self._run, event = False, 0, VadEvent.SPEECH_END
        return VadResult(voice, event, db)

"""Diagnóstico da entrada de voz (ferramenta de desenvolvimento). Desligado por padrão.

Com `debug=True` registra, por trecho: tipo, duração, níveis (dBFS), clipping, ruído de fundo, taxa do
dispositivo, tempo e confiança da inferência e o TEXTO retornado. NUNCA registra áudio. Gravar .wav é opt-in
(`debug_audio_dir`), guarda só os últimos trechos e apaga os antigos."""
from __future__ import annotations

import logging
import wave
from collections import deque
from pathlib import Path

import numpy as np

from app.core.config import VoiceConfig

log = logging.getLogger("jarvis.voice")
KEEP_FILES = 8


def audio_stats(audio: np.ndarray, sample_rate: int = 16000) -> dict:
    x = audio.astype(np.float32) / 32768.0
    rms = float(np.sqrt((x ** 2).mean())) if len(x) else 0.0
    peak = float(np.abs(x).max()) if len(x) else 0.0
    db = lambda v: round(20 * np.log10(max(v, 1e-9)), 1)
    return {"duration_s": round(len(audio) / sample_rate, 2), "rms_dbfs": db(rms), "peak_dbfs": db(peak),
            "clipped_pct": round(100 * float((np.abs(audio) >= 32760).mean()), 2) if len(audio) else 0.0}


class VoiceDiagnostics:
    def __init__(self, cfg: VoiceConfig) -> None:
        self.enabled, self._rate = cfg.debug, cfg.sample_rate
        self._dir = Path(cfg.debug_audio_dir) if cfg.debug and cfg.debug_audio_dir else None
        self._files: deque[Path] = deque()
        self.last: dict = {}
        if self._dir is not None:
            self._dir.mkdir(parents=True, exist_ok=True)
            for old in self._dir.glob("jarvis_debug_*.wav"):  # não deixa gravações de execuções anteriores
                old.unlink()

    def record(self, kind: str, audio: np.ndarray, text: str, infer_s: float, info: dict | None = None,
               noise_db: float | None = None, audio_service=None, note: str = "") -> None:
        if not self.enabled:
            return
        stats = audio_stats(audio, self._rate)
        self.last = dict(stats, kind=kind, text=text, infer_s=round(infer_s, 2), noise_floor_db=noise_db, **(info or {}))
        dev = f" dispositivo={audio_service.device_name!r} taxa={audio_service.native_rate}Hz" \
              f"{' (convertida)' if audio_service.resampling else ''}" if audio_service is not None and getattr(audio_service, "device_name", "") else ""
        log.info("[VOICE][DEBUG] %s: %.2fs rms=%sdBFS pico=%sdBFS clip=%s%% ruído=%s inferência=%.2fs conf=%s%s texto=%r %s",
                 kind, stats["duration_s"], stats["rms_dbfs"], stats["peak_dbfs"], stats["clipped_pct"],
                 None if noise_db is None else round(noise_db, 1), infer_s, (info or {}).get("avg_logprob"), dev, text, note)
        if self._dir is not None:
            self._save(kind, audio)

    def _save(self, kind: str, audio: np.ndarray) -> None:
        path = self._dir / f"jarvis_debug_{kind}_{int(np.random.default_rng().integers(1e9))}.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self._rate)
            w.writeframes(audio.astype(np.int16).tobytes())
        self._files.append(path)
        while len(self._files) > KEEP_FILES:
            self._files.popleft().unlink(missing_ok=True)

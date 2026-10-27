"""Speech-to-text local (faster-whisper). Áudio -> texto; não conhece IA, wake word nem UI."""
from __future__ import annotations

import logging
import re
import threading

import numpy as np

from app.core.config import VoiceConfig

log = logging.getLogger("jarvis.voice")

# Frases que o Whisper inventa sobre silêncio/ruído.
_HALLUCINATIONS = ("legendas pela comunidade amara.org", "legenda adriana zanotto", "inscreva-se no canal", "obrigado por assistir")


class STTError(Exception):
    """Falha ao carregar o modelo ou transcrever."""


class ModelNotFoundError(STTError):
    """Modelo de reconhecimento de voz ausente no disco."""


class STTService:
    def __init__(self, cfg: VoiceConfig) -> None:
        self._cfg = cfg
        self._model = None
        self._lock = threading.Lock()
        self.last_info: dict = {}  # confiança e idioma da última transcrição (só para diagnóstico)

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def load(self) -> None:
        """Carrega o modelo uma única vez (chamadas seguintes não fazem nada). Nunca baixa nada."""
        with self._lock:
            if self._model is not None:
                return
            path = self._cfg.stt_model_path
            if not (path / "model.bin").is_file():
                raise ModelNotFoundError(
                    f"Modelo de reconhecimento de voz não encontrado em {path}. "
                    f"Baixe com: python scripts/download_model.py {self._cfg.stt_model}")
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise STTError("Pacote 'faster-whisper' não instalado. Rode: pip install -r requirements.txt") from exc
            try:
                self._model = WhisperModel(str(path), device="cpu", compute_type="int8",
                                           cpu_threads=self._cfg.stt_threads, local_files_only=True)
            except (RuntimeError, OSError, ValueError) as exc:
                raise STTError(f"Falha ao carregar o modelo de voz: {exc}") from exc
            log.info("[VOICE] Modelo de voz carregado: %s", path.name)

    def transcribe(self, audio: np.ndarray, fast: bool = False) -> str:
        """int16 mono 16 kHz -> texto pt-BR. `fast` (beam 1) é usado na checagem da wake word."""
        self.load()
        if len(audio) < self._cfg.sample_rate * 0.3:
            return ""
        samples = audio.astype(np.float32) / 32768.0
        try:
            segments, info = self._model.transcribe(
                samples, language=self._cfg.language, beam_size=1 if fast else self._cfg.stt_beam_size,
                vad_filter=False, condition_on_previous_text=False, temperature=0.0,
                no_speech_threshold=0.6, compression_ratio_threshold=2.4, initial_prompt=self._cfg.stt_prompt or None)
            kept = [s for s in segments if not (s.no_speech_prob > 0.6 and s.avg_logprob < -1.0)]
            parts = [s.text.strip() for s in kept]
            self.last_info = {"avg_logprob": round(sum(s.avg_logprob for s in kept) / len(kept), 2) if kept else None,
                              "language": getattr(info, "language", None),
                              "language_prob": round(getattr(info, "language_probability", 0.0), 2)}
        except (RuntimeError, ValueError) as exc:
            raise STTError(f"Falha na transcrição: {exc}") from exc
        text = " ".join(p for p in parts if p).strip()
        if any(h in text.lower() for h in _HALLUCINATIONS):
            return ""
        prompt = self._cfg.stt_prompt
        if prompt and re.sub(r"\W+", " ", text.lower()).strip() and re.sub(r"\W+", " ", text.lower()).strip() in re.sub(r"\W+", " ", prompt.lower()):
            return ""  # o modelo só repetiu o vocabulário do prompt (alucinação sobre ruído)
        return text

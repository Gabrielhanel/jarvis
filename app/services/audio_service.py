"""Captura do microfone (sounddevice/PortAudio). Não conhece VAD, STT, IA nem a UI."""
from __future__ import annotations

import logging
import queue
from dataclasses import dataclass

import numpy as np

from app.core.config import VoiceConfig

log = logging.getLogger("jarvis.voice")


class AudioError(Exception):
    """Falha de áudio (driver, dispositivo ocupado, permissão, taxa incompatível)."""


class MicrophoneUnavailable(AudioError):
    """Nenhum microfone utilizável."""


@dataclass(frozen=True)
class InputDevice:
    index: int
    name: str
    default_samplerate: float
    is_default: bool


def _portaudio():
    try:
        import sounddevice as sd
    except OSError as exc:  # biblioteca nativa ausente
        raise AudioError("Biblioteca de áudio (PortAudio) não encontrada. No Linux: sudo apt install libportaudio2") from exc
    except ImportError as exc:
        raise AudioError("Pacote 'sounddevice' não instalado. Rode: pip install -r requirements.txt") from exc
    return sd


def list_input_devices() -> list[InputDevice]:
    sd = _portaudio()
    try:
        devices = sd.query_devices()
        default = sd.default.device[0]
    except (sd.PortAudioError, ValueError) as exc:
        raise AudioError(f"Não foi possível listar os dispositivos de áudio: {exc}") from exc
    return [InputDevice(i, d["name"], float(d["default_samplerate"]), i == default)
            for i, d in enumerate(devices) if d["max_input_channels"] > 0]


def refresh_devices() -> None:
    """Reinicia o PortAudio para enxergar microfones conectados depois da abertura do app."""
    try:
        sd = _portaudio()
    except AudioError:
        return  # sem PortAudio não há o que atualizar; start() reporta o erro ao usuário
    try:
        sd._terminate()
        sd._initialize()
    except (AttributeError, sd.PortAudioError) as exc:
        log.debug("[VOICE] Não foi possível atualizar a lista de dispositivos: %s", exc)


class _Reframer:
    """Entrega frames de tamanho fixo. Se a taxa do microfone não for a do modelo, converte de verdade
    com um reamostrador em streaming (soxr, filtro anti-aliasing), preservando o estado entre blocos."""

    def __init__(self, src_rate: int, dst_rate: int, frame: int) -> None:
        self._frame, self._buf, self._stream = frame, np.zeros(0, np.int16), None
        if src_rate != dst_rate:
            try:
                import soxr
            except ImportError as exc:
                raise AudioError("Pacote 'soxr' não instalado (necessário quando o microfone não aceita 16 kHz). "
                                 "Rode: pip install -r requirements.txt") from exc
            self._stream = soxr.ResampleStream(src_rate, dst_rate, 1, dtype="int16", quality="HQ")

    def push(self, block: np.ndarray) -> list[np.ndarray]:
        if self._stream is not None:
            block = self._stream.resample_chunk(block)
        self._buf = np.concatenate([self._buf, block]) if len(self._buf) else block
        out = []
        while len(self._buf) >= self._frame:
            out.append(self._buf[: self._frame].copy())
            self._buf = self._buf[self._frame:]
        return out


class AudioService:
    """Entrega frames int16 mono de `frame_ms` na taxa configurada, via fila."""

    def __init__(self, cfg: VoiceConfig) -> None:
        self._cfg = cfg
        self._queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=max(10, 10_000 // cfg.frame_ms))
        self._stream = None
        self._name = ""
        self._rate = cfg.sample_rate
        self._reframer = _Reframer(cfg.sample_rate, cfg.sample_rate, cfg.frame_samples)
        self.dropped_frames = 0
        self.device_name, self.native_rate = "", cfg.sample_rate  # para o diagnóstico

    @property
    def resampling(self) -> bool:
        return self.native_rate != self._cfg.sample_rate

    @property
    def is_active(self) -> bool:
        return self._stream is not None

    def refresh(self) -> None:
        if self._stream is None:
            refresh_devices()

    def start(self) -> str:
        """Abre o stream e devolve o nome do microfone. Levanta AudioError/MicrophoneUnavailable."""
        if self._stream is not None:
            return self._name
        sd = _portaudio()
        devices = list_input_devices()
        if not devices:
            raise MicrophoneUnavailable("Microfone não encontrado")
        device = self._pick(devices)
        last_error: Exception | None = None
        for rate in dict.fromkeys((self._cfg.sample_rate, int(device.default_samplerate))):  # 16 kHz, senão o nativo
            stream = None
            self._reframer = _Reframer(rate, self._cfg.sample_rate, self._cfg.frame_samples)
            try:
                stream = sd.InputStream(device=device.index, channels=1, samplerate=rate, dtype="int16",
                                        blocksize=rate * self._cfg.frame_ms // 1000, callback=self._callback)
                stream.start()
            except sd.PortAudioError as exc:
                last_error = exc
                if stream is not None:
                    stream.close()
                continue
            self._stream, self._rate, self._name = stream, rate, device.name
            self.device_name, self.native_rate = device.name, rate
            if rate != self._cfg.sample_rate:
                log.info("[VOICE] O microfone não aceitou %d Hz: convertendo de %d Hz com filtro anti-aliasing", self._cfg.sample_rate, rate)
            log.info("[VOICE] Microfone inicializado: %s (%d Hz)", device.name, rate)
            return device.name
        raise AudioError(self._explain(last_error))

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            sd = _portaudio()
            try:
                stream.stop()
                stream.close()
            except sd.PortAudioError as exc:
                log.warning("[VOICE] Erro ao fechar o microfone: %s", exc)
        while not self._queue.empty():
            self._queue.get_nowait()

    def read(self, timeout: float = 0.25) -> np.ndarray | None:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    # --- interno ---
    def _pick(self, devices: list[InputDevice]) -> InputDevice:
        wanted = self._cfg.device
        if wanted is None:
            return next((d for d in devices if d.is_default), devices[0])
        for d in devices:
            if (isinstance(wanted, int) and d.index == wanted) or (isinstance(wanted, str) and wanted.lower() in d.name.lower()):
                return d
        raise MicrophoneUnavailable(f"Microfone '{wanted}' não encontrado")

    def _callback(self, indata, frames, time_info, status) -> None:  # thread do PortAudio: sem Qt, sem log pesado
        for frame in self._reframer.push(indata[:, 0].copy()):
            if self._cfg.input_gain != 1.0:
                frame = np.clip(frame.astype(np.float32) * self._cfg.input_gain, -32768, 32767).astype(np.int16)
            try:
                self._queue.put_nowait(frame)
            except queue.Full:
                self.dropped_frames += 1
                try:
                    self._queue.get_nowait()  # descarta o mais antigo
                    self._queue.put_nowait(frame)
                except (queue.Empty, queue.Full):
                    pass  # outra thread esvaziou/encheu a fila no mesmo instante; o frame é só descartado

    @staticmethod
    def _explain(exc: Exception | None) -> str:
        text = str(exc or "erro desconhecido")
        low = text.lower()
        if "sample rate" in low:
            return f"Taxa de amostragem incompatível com o microfone ({text})"
        if "busy" in low or "unavailable" in low:
            return f"Microfone ocupado por outro programa ({text})"
        if "denied" in low or "permission" in low:
            return "Permissão de microfone negada. Libere o acesso nas configurações de privacidade do sistema."
        return f"Não foi possível abrir o microfone: {text}"

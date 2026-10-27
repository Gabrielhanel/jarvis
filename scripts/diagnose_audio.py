"""Diagnóstico do microfone: níveis, ruído, clipping, taxa e comportamento da VAD.

Uso:  python scripts/diagnose_audio.py [--seconds 6] [--device N|parte-do-nome] [--save trecho.wav]
Fale a frase "Abra o Spotify" normalmente quando pedido. Nada é gravado em disco sem --save."""
import argparse
import sys
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core.config import VoiceConfig  # noqa: E402
from app.services.audio_service import AudioError, AudioService, list_input_devices  # noqa: E402
from app.services.vad_service import VADService, VadEvent  # noqa: E402
from app.services.voice_debug import audio_stats  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=6.0)
    ap.add_argument("--device", default=None)
    ap.add_argument("--save", default=None)
    args = ap.parse_args()
    device = int(args.device) if args.device and args.device.isdigit() else args.device
    try:
        print("Microfones:")
        for d in list_input_devices():
            print(f"  [{d.index}] {d.name}  ({int(d.default_samplerate)} Hz){'  <- padrão' if d.is_default else ''}")
        cfg = VoiceConfig(device=device)
        audio = AudioService(cfg)
        name = audio.start()
    except AudioError as exc:
        print(f"ERRO: {exc}")
        return 1
    print(f"\nUsando: {name} | taxa nativa {audio.native_rate} Hz | {'CONVERTIDA para 16 kHz (soxr)' if audio.resampling else 'direto em 16 kHz'}")
    print("Fique em silêncio por 1 s, depois diga 'Abra o Spotify'...")
    vad, frames, events, t0 = VADService(cfg), [], [], time.monotonic()
    while time.monotonic() - t0 < args.seconds:
        frame = audio.read(0.25)
        if frame is None:
            continue
        frames.append(frame)
        ev = vad.process(frame).event
        if ev is not VadEvent.NONE:
            events.append((ev, len(frames) * cfg.frame_ms / 1000))
    audio.stop()
    if not frames:
        print("ERRO: nenhum áudio recebido do dispositivo.")
        return 1
    x = np.concatenate(frames)
    st = audio_stats(x)
    per = np.array([np.sqrt(np.mean((f.astype(np.float32) / 32768) ** 2)) for f in frames])
    noise_db = 20 * np.log10(max(float(np.percentile(per, 20)), 1e-9))
    speech_db = 20 * np.log10(max(float(np.percentile(per, 95)), 1e-9))
    print(f"\nDuração {st['duration_s']}s | RMS geral {st['rms_dbfs']} dBFS | pico {st['peak_dbfs']} dBFS | clipping {st['clipped_pct']}%")
    print(f"Ruído de fundo ~{noise_db:.1f} dBFS | fala (p95) ~{speech_db:.1f} dBFS | SNR ~{speech_db - noise_db:.1f} dB | piso da VAD {vad.noise_floor_db:.1f} dB")
    print("Eventos da VAD:", [(e.name, f"{t:.2f}s") for e, t in events] or "nenhum (nenhuma fala detectada)")
    warns = []
    if speech_db < -38:
        warns.append("Volume de entrada MUITO BAIXO: aumente o ganho do microfone no sistema ou chegue mais perto.")
    if st["clipped_pct"] > 0.5:
        warns.append("Áudio ESTOURANDO (clipping): diminua o ganho do microfone.")
    if speech_db - noise_db < 15:
        warns.append("SNR baixo (<15 dB): muito ruído ou fala baixa; o reconhecimento tende a piorar.")
    if not events:
        warns.append("A VAD não detectou fala: fale mais alto/perto ou reduza `sensitivity` mínimo em VoiceConfig.")
    print("\nAvisos:" if warns else "\nNenhum problema evidente de captura.")
    for w in warns:
        print(" -", w)
    if args.save:
        with wave.open(args.save, "wb") as w:
            w.setnchannels(1), w.setsampwidth(2), w.setframerate(cfg.sample_rate)
            w.writeframes(x.tobytes())
        print(f"\nTrecho salvo em {args.save} (arquivo local; apague quando não precisar mais).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

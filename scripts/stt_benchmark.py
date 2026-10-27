"""Benchmark reproduzível do reconhecimento de voz (STT) com as frases de referência.

  python scripts/stt_benchmark.py                       # interativo: usa o microfone e o MESMO pipeline do app
  python scripts/stt_benchmark.py --wav-dir pasta       # avalia arquivos .wav (01_*.wav = frase 1 ...), sem microfone
  opções: --attempts 5  --model small|base  --csv resultado.csv  --save-wav pasta (opt-in, só no modo interativo)

Mede o WER (taxa de erro de palavras) contra a frase pretendida. Nada é corrigido por regras: o texto exibido
é exatamente o que o STT devolveu. Compare modelos rodando duas vezes com --model diferente."""
import argparse
import csv
import re
import sys
import threading
import time
import unicodedata
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PHRASES = ["Abra o Spotify.", "Abra o navegador.", "Abra o Visual Studio Code.", "Qual é a previsão do tempo?",
           "Que compromissos eu tenho hoje?", "Quais são as notícias de hoje?",
           "Hoje preciso estudar programação e fazer um trabalho da faculdade."]


def words(text: str) -> list[str]:
    d = unicodedata.normalize("NFD", text.lower())
    return re.sub(r"[^a-z0-9 ]", " ", "".join(c for c in d if unicodedata.category(c) != "Mn")).split()


def wer(ref: str, hyp: str) -> float:
    r, h = words(ref), words(hyp)
    d = list(range(len(h) + 1))
    for i, a in enumerate(r, 1):
        prev, d[0] = d[0], i
        for j, b in enumerate(h, 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (a != b))
    return d[-1] / max(1, len(r))


def verdict(w: float) -> str:
    return "correto" if w == 0 else "parcialmente correto" if w <= 0.34 else "incorreto"


def read_wav16k(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        raw = np.frombuffer(w.readframes(w.getnframes()), np.int16).reshape(-1, w.getnchannels())[:, 0]
        rate = w.getframerate()
    if rate != 16000:
        import soxr
        raw = soxr.resample(raw, rate, 16000).astype(np.int16)
    return raw


def evaluate_wavs(transcribe, wav_dir: Path, phrases=PHRASES) -> list[dict]:
    from app.services.voice_debug import audio_stats
    rows = []
    for path in sorted(wav_dir.glob("*.wav")):
        m = re.match(r"(\d+)", path.name)
        if not m or not 1 <= int(m.group(1)) <= len(phrases):
            continue
        audio, ref = read_wav16k(path), phrases[int(m.group(1)) - 1]
        t0 = time.monotonic()
        text = transcribe(audio)
        st, w = audio_stats(audio), wer(ref, text)
        obs = "; ".join(x for x, c in (("nível baixo", st["rms_dbfs"] < -38), ("clipping", st["clipped_pct"] > 0.5),
                                      ("áudio curto", st["duration_s"] < 0.6)) if c)
        rows.append(dict(arquivo=path.name, pretendida=ref, obtida=text, duracao_s=st["duration_s"], wer=round(w, 2),
                         resultado=verdict(w), observacao=obs, inferencia_s=round(time.monotonic() - t0, 2)))
    return rows


def print_table(rows: list[dict]) -> None:
    print(f"\n{'Frase pretendida':36} | {'Transcrição obtida':36} | {'Dur.':>5} | Resultado")
    for r in rows:
        print(f"{r['pretendida'][:36]:36} | {r['obtida'][:36]:36} | {r['duracao_s']:5.1f} | {r['resultado']} {('(' + r['observacao'] + ')') if r['observacao'] else ''}")
    if rows:
        print(f"\nWER médio: {sum(r['wer'] for r in rows) / len(rows):.2f} | corretas: {sum(r['resultado'] == 'correto' for r in rows)}/{len(rows)}"
              f" | inferência média: {sum(r['inferencia_s'] for r in rows) / len(rows):.2f}s")


def write_csv(rows: list[dict], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["arquivo"])
        w.writeheader()
        w.writerows(rows)


def run_interactive(cfg, attempts: int, save_dir: str | None) -> list[dict]:
    from app.services.audio_service import AudioError, AudioService
    from app.services.stt_service import STTError, STTService
    from app.services.vad_service import VADService
    from app.services.voice_service import VoiceEvent, VoicePipeline
    from app.services.wakeword_service import WakeWordService
    stt = STTService(cfg)
    events, got = [], threading.Event()

    def emit(ev, *a):
        events.append((ev, a))
        if ev in (VoiceEvent.TRANSCRIPTION_FINISHED, VoiceEvent.TRANSCRIPTION_ERROR, VoiceEvent.COMMAND_TIMEOUT):
            got.set()
    from dataclasses import replace
    cfg = replace(cfg, debug=True, debug_audio_dir=save_dir)
    pipe = VoicePipeline(cfg, AudioService(cfg), VADService(cfg), stt, WakeWordService(lambda a: stt.transcribe(a, fast=True), cfg), emit)
    thread = threading.Thread(target=pipe.run, daemon=True)
    thread.start()
    time.sleep(1.5)
    rows = []
    try:
        for ref in PHRASES:
            for n in range(1, attempts + 1):
                input(f"\n[{ref}]  tentativa {n}/{attempts} - tecle Enter e fale a frase... ")
                events.clear(); got.clear()
                pipe.trigger_manual()
                got.wait(20)
                text = next((a[0] for ev, a in events if ev == VoiceEvent.TRANSCRIPTION_FINISHED), "")
                dur = pipe.diag.last.get("duration_s", 0.0)
                w = wer(ref, text)
                rows.append(dict(arquivo="mic", pretendida=ref, obtida=text, duracao_s=dur, wer=round(w, 2), resultado=verdict(w),
                                 observacao="", inferencia_s=pipe.diag.last.get("infer_s", 0.0)))
                print(f"   -> {text!r}  [{verdict(w)}]")
    except (KeyboardInterrupt, EOFError):
        print("\nInterrompido.")
    finally:
        pipe.stop()
        thread.join(5)
    return rows


def main() -> int:
    from app.core.config import VoiceConfig
    from app.services.stt_service import STTError, STTService
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav-dir")
    ap.add_argument("--attempts", type=int, default=5)
    ap.add_argument("--model", default=VoiceConfig().stt_model)
    ap.add_argument("--csv")
    ap.add_argument("--save-wav")
    args = ap.parse_args()
    cfg = VoiceConfig(stt_model=args.model)
    print(f"Modelo STT: faster-whisper '{args.model}' ({cfg.stt_model_path})")
    try:
        if args.wav_dir:
            stt = STTService(cfg)
            stt.load()
            rows = evaluate_wavs(lambda a: stt.transcribe(a), Path(args.wav_dir))
        else:
            rows = run_interactive(cfg, args.attempts, args.save_wav)
    except STTError as exc:
        print(f"ERRO: {exc}")
        return 1
    print_table(rows)
    if args.csv:
        write_csv(rows, args.csv)
        print(f"CSV: {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

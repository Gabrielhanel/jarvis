"""Baixa (uma vez) o modelo de voz do Jarvis (Kokoro-82M, ONNX) para assets/models/kokoro/.

Uso:  python scripts/download_tts_model.py [int8|fp32] [--force]
  int8  ~92 MB, mais rápido (padrão)       fp32  ~310 MB, mais fiel; depois aponte tts.model_path
                                            para kokoro-v1.0.onnx em data/settings.json.
O Jarvis nunca baixa modelos sozinho; depois do download ele fala sem internet.
Origem: github.com/thewh1teagle/kokoro-onnx (release model-files-v1.0). Licença do modelo: Apache-2.0.
"""
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
VARIANTS = {"int8": ["kokoro-v1.0.int8.onnx", "voices-v1.0.bin"], "fp32": ["kokoro-v1.0.onnx", "voices-v1.0.bin"]}
DEST = Path(__file__).resolve().parents[1] / "assets" / "models" / "kokoro"


def download(name: str, force: bool) -> None:
    target = DEST / name
    if target.is_file() and not force:
        print(f"{name}: já existe ({target.stat().st_size // 2**20} MB), pulando.")
        return
    part = target.with_suffix(target.suffix + ".part")
    print(f"Baixando {name} ...")
    with urllib.request.urlopen(f"{BASE}/{name}", timeout=60) as resp, open(part, "wb") as out:
        total, done = int(resp.headers.get("Content-Length") or 0), 0
        while chunk := resp.read(1 << 20):
            out.write(chunk)
            done += len(chunk)
            if total:
                print(f"\r  {done * 100 // total}%", end="", flush=True)
    print()
    if part.stat().st_size < 1 << 20:
        part.unlink()
        raise OSError(f"{name}: arquivo baixado é pequeno demais (download incompleto)")
    part.replace(target)


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    variant = args[0] if args else "int8"
    if variant not in VARIANTS:
        print(f"Variante inválida: {variant}. Use int8 ou fp32.")
        return 2
    DEST.mkdir(parents=True, exist_ok=True)
    try:
        for name in VARIANTS[variant]:
            download(name, "--force" in argv)
    except (urllib.error.URLError, OSError) as exc:
        print(f"Falha no download: {exc}")
        return 1
    print(f"Pronto: {DEST}. Reinicie o Jarvis.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

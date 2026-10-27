"""Baixa UMA vez o modelo de voz (Whisper convertido para CTranslate2) para assets/models/.

Uso:  python scripts/download_model.py [tiny|base|small|medium]     (padrão: small)
O Jarvis nunca baixa modelos sozinho; depois do download ele funciona sem internet.
"""
import sys
from pathlib import Path

SIZES = ("tiny", "base", "small", "medium")


def main(argv: list[str]) -> int:
    size = argv[1] if len(argv) > 1 else "small"
    if size not in SIZES:
        print(f"Tamanho inválido: {size}. Use um de {', '.join(SIZES)}.")
        return 2
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("Instale as dependências primeiro: pip install -r requirements.txt")
        return 1
    dest = Path(__file__).resolve().parents[1] / "assets" / "models" / f"faster-whisper-{size}"
    print(f"Baixando Systran/faster-whisper-{size} para {dest} ...")
    try:
        snapshot_download(f"Systran/faster-whisper-{size}", local_dir=dest)
    except OSError as exc:  # falhas de rede/HTTP do huggingface_hub herdam de OSError
        print(f"Falha no download: {exc}")
        return 1
    print("Pronto. Reinicie o Jarvis.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

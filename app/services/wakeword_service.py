"""Wake word "Ei Jarvis". Responde apenas: o usuário chamou o Jarvis?

LIMITAÇÃO REAL: não é um modelo acústico de keyword spotting. O áudio de uma fala curta é
transcrito localmente (STT) e o texto é comparado com tolerância a variações. Isso exige o
fim da frase (latência) e roda o STT a cada trecho de fala curto. O mecanismo é trocável:
basta outra classe com `check_audio(audio) -> WakeResult`.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Callable

import numpy as np

from app.core.config import VoiceConfig

PREFIXES = {"ei", "hey", "ey", "eh", "ai", "oi", "hei", "e", "a"}
NAME_ALIASES = {"jarvis", "jarves", "jarvez", "javis", "charvis", "garvis", "zarvis", "jarbis", "jervis", "jarvice", "jarvi"}
MAX_TOKENS_BEFORE_NAME = 3  # a wake word precisa abrir a frase (evita "eu disse ei jarvis ontem")


@dataclass(frozen=True)
class WakeResult:
    found: bool
    remainder: str = ""  # o que veio depois da wake word na mesma frase ("Ei Jarvis, abra o Spotify")


def _norm(token: str) -> str:
    decomposed = unicodedata.normalize("NFD", token.lower())
    return re.sub(r"[^a-z0-9]", "", "".join(c for c in decomposed if unicodedata.category(c) != "Mn"))


def _lev(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _is_name(word: str, prefixed: bool = False) -> bool:
    """Com um "ei/hey" antes, o contexto já indica a chamada: aceita até 2 erros ("Jarbes", "Garvis").
    Sem prefixo, só 1 erro, para não ativar com palavras comuns."""
    if word in NAME_ALIASES:
        return True
    dist = _lev(word, "jarvis") if 4 <= len(word) <= 8 else 99
    return dist <= 1 or (prefixed and len(word) >= 5 and dist <= 2)


class WakeWordService:
    def __init__(self, transcribe: Callable[[np.ndarray], str], cfg: VoiceConfig) -> None:
        self._transcribe = transcribe
        self._cfg = cfg
        self.last_text = ""  # o que o STT ouviu na última checagem (diagnóstico)

    def check_audio(self, audio: np.ndarray) -> WakeResult:
        self.last_text = self._transcribe(audio)
        return self.match(self.last_text)

    def match(self, text: str) -> WakeResult:
        tokens = [(_norm(m.group()), m.end()) for m in re.finditer(r"\S+", text)]
        for i, (word, end) in enumerate(tokens[: MAX_TOKENS_BEFORE_NAME + 1]):
            after_prefix = i > 0 and tokens[i - 1][0] in PREFIXES
            joined = any(word.startswith(p) and _is_name(word[len(p):], True) for p in PREFIXES)  # "eijarvis"
            spaced = after_prefix and _is_name(word, True)
            split = after_prefix and i + 1 < len(tokens) and _is_name(word + tokens[i + 1][0], True)  # "já vis"
            alone = i == 0 and self._cfg.wake_allow_name_only and _is_name(word)
            if joined or spaced or alone or split:
                end = tokens[i + 1][1] if split and not (joined or spaced or alone) else end
                return WakeResult(True, text[end:].lstrip(" ,.:;!?-–…").strip())
        return WakeResult(False)

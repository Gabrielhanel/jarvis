"""Texto sem acento/maiúsculas, palavras úteis e sinais de segredo. Puro e testável: nada aqui toca banco, UI ou rede."""
from __future__ import annotations

import re
import unicodedata

STOPWORDS = frozenset("""
a o as os um uma uns umas de do da dos das em no na nos nas por para pra com sem sob sobre entre ate
e ou mas que se como quando onde qual quais quem porque pois entao ja ainda tambem mesmo mais menos muito pouco
eu me meu minha meus minhas voce vc seu sua seus suas ele ela eles elas nos lhe isso isto esse essa esses essas
este esta estes estas aquele aquela ao aos aquilo ser sao foi era sera estou esta estao estar ter tem tenho temos
fazer faz faco pode posso quero preciso gostaria algo alguma algum nada tudo todo toda todos todas cada
jarvis lembre lembra guarde anote memoria memorias favor oi ola
melhor forma coisa coisas exemplo ajuda ajude ajudar usar uso explique explicar tipo hoje agora bom boa bem fazendo
""".split())

_SECRET_WORDS = re.compile(r"\b(senha|senhas|password|passwd|api ?key|chave (de|da) api|segredo|secret|chave privada|private key|"
                           r"codigo de seguranca|cvv|pin do cartao)\b")
_TOKEN_VALUE = re.compile(r"\b(token|bearer)\b[^.\n]{0,25}?(:|=|\be\b|\beh\b|\bis\b)\s*\S{6,}")
_KEY_SHAPES = re.compile(
    r"sk-ant-[A-Za-z0-9_\-]{8,}|sk-[A-Za-z0-9]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|"
    r"eyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|AIza[0-9A-Za-z_\-]{30,}")


def fold_with_map(text: str) -> tuple[str, list[int]]:
    """Minúsculas e sem acentos, preservando o alinhamento: `idx[i]` é a posição, em `text`, do caractere `i` do resultado."""
    chars: list[str] = []
    idx: list[int] = []
    for i, ch in enumerate(text):
        for c in unicodedata.normalize("NFD", ch):
            if unicodedata.category(c) != "Mn":
                chars.append(c.lower()[:1])
                idx.append(i)
    return "".join(chars), idx


def fold(text: str) -> str:
    return fold_with_map(text)[0]


def stem(word: str) -> str:
    return word[:-1] if len(word) > 4 and word.endswith("s") else word  # plural simples: "respostas" ~ "resposta"


def tokens(text: str) -> list[str]:
    """Palavras com conteúdo (sem acento, sem palavras comuns, plural simplificado), sem repetição, na ordem em que aparecem."""
    seen: dict[str, None] = {}
    for w in re.findall(r"[a-z0-9]+", fold(text)):
        if len(w) >= 3 and w not in STOPWORDS:
            seen.setdefault(stem(w), None)
    return list(seen)


def words_match(a: str, b: str) -> bool:
    return a == b or (min(len(a), len(b)) >= 5 and (a.startswith(b) or b.startswith(a)))


def jaccard(a: list[str], b: list[str]) -> float:
    sa, sb = set(a), set(b)
    return len(sa & sb) / len(sa | sb) if sa and sb else 0.0


def looks_like_secret(text: str) -> bool:
    """Conservador de propósito: senhas, chaves de API e tokens NÃO viram memória permanente."""
    folded = fold(text)
    return bool(_KEY_SHAPES.search(text) or _SECRET_WORDS.search(folded) or _TOKEN_VALUE.search(folded))


_ALIAS_NOISE = frozenset("o a os as um uma meu minha meus minhas de do da dos das no na nos nas ao aos".split())


def norm_alias(text: str) -> str:
    """Forma comparável de um nome de destino ("O Spotify", "meu projeto") sem acento, sem artigos/possessivos."""
    return " ".join(w for w in re.findall(r"[a-z0-9]+", fold(text)) if w not in _ALIAS_NOISE)

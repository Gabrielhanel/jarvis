"""Interpreta pedidos de controle do computador na mensagem do USUÁRIO (texto ou transcrição de voz). Determinístico, sem IA.

Só reconhece frases IMPERATIVAS no início ("Abra o Spotify", "Feche o aplicativo X", "Pesquise ... no Spotify").
"Como abrir o Spotify pelo Python?" segue para a IA. O resultado é só uma INTENÇÃO com texto não confiável: quem decide se o
destino é permitido é o ActionService, contra a lista autorizada. Nada aqui executa algo."""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.intents import _M_PREFIX, _norm
from app.core.textutil import fold_with_map, norm_alias


@dataclass(frozen=True)
class ActionParse:
    op: str  # "open" | "close" | "spotify"
    noun: str = ""  # open: "app" | "pasta" | "site" | "projeto" | "" (genérico: procura em todas as listas)
    target: str = ""  # alias normalizado (sem acento, sem artigos); texto NÃO confiável
    raw: str = ""  # trecho original (acentos e maiúsculas): URL digitada ou texto da pesquisa


_OPEN = re.compile(r"^(?:(?:me|pra mim|para mim)\s+)?(?:abra|abre|abrir|inicie|iniciar|lance|lancar)\b(?P<body>.*)$", re.S)
_CLOSE = re.compile(r"^(?:(?:feche|fecha|fechar)\b|(?:encerre|encerrar|finalize|finalizar|termine|terminar)(?=\s+(?:o\s+|um\s+)?(?:aplicativo|app|programa)\b))(?P<body>.*)$", re.S)
_VERBS = r"(?:pesquise|pesquisa|pesquisar|procure|procura|procurar|busque|busca|buscar)"
_SPOT_A = re.compile(rf"^{_VERBS}\s+(?P<q>.+?)\s+(?:no|na|pelo|em|do)\s+(?:o\s+)?spotify[\s.!?]*$", re.S)
_SPOT_B = re.compile(rf"^{_VERBS}\s+(?P<q>(?:(?:a|o|as|os)\s+)?(?:musica|cancao|artista|banda|album|playlist|cantor|cantora|podcast)\b.*)$", re.S)
_SPOT_C = re.compile(rf"^(?:no\s+)?spotify[,:]?\s+{_VERBS}\s+(?P<q>.+)$", re.S)
_SPOT_NOUN = re.compile(r"^(?:(?:a|o|as|os)\s+)?(?:musica|cancao|artista|banda|album|playlist|cantor|cantora|podcast)\b\s*(?:(?:de|do|da)\s+)?")
_DET = r"(?:(?:o|a|os|as|um|uma|meu|minha|meus|minhas)\s+)*"
_NOUNS = (
    ("pasta", r"(?:pasta|diretorio|folder)"),
    ("site", r"(?:site|sites|pagina|url|link|endereco)"),
    ("projeto", r"projeto"),
    ("app", r"(?:aplicativo|app|programa|aplicacao)"),
)
_TRAILING = re.compile(r"(?:\s+(?:pra|para)\s+mim|\s+por favor|\s+agora)+[\s.!?]*$")


def _orig(text: str, idx: list[int], pos: int, end: int | None = None) -> str:
    """Trecho de `text` (grafia original) que corresponde às posições [pos, end) do texto sem acento."""
    if pos >= len(idx):
        return ""
    stop = len(text) if end is None or end >= len(idx) else idx[end]
    return text[idx[pos]:stop]


def _split_body(body: str, orig_body: str, bidx: list[int], with_nouns: bool = True) -> tuple[str, str, str]:
    """(substantivo, alvo normalizado, trecho original do alvo) a partir do que vem depois do verbo."""
    cleaned = _TRAILING.sub("", body.rstrip())
    for noun, pattern in (_NOUNS if with_nouns else ()):
        m = re.match(rf"\s*{_DET}{pattern}\b\s*(?:(?:de|do|da|dos|das|web)\s+)?(?P<t>.*)$", cleaned, re.S)
        if m:
            return noun, norm_alias(m.group("t")), _orig(orig_body, bidx, m.start("t"), len(cleaned)).strip(" \t\n.!?")
    m = re.match(rf"\s*{_DET}(?P<t>.*)$", cleaned, re.S)
    t_start = m.start("t") if m else 0
    return "", norm_alias(cleaned[t_start:]), _orig(orig_body, bidx, t_start, len(cleaned)).strip(" \t\n.!?")


def classify_action(text: str) -> ActionParse | None:
    folded, idx = fold_with_map(text or "")
    base = _M_PREFIX.match(folded).end()
    rest = folded[base:]
    if not rest.strip():
        return None

    def original(pos: int) -> str:
        return text[idx[base + pos]:] if base + pos < len(idx) else ""

    # Spotify: "Pesquise Legião Urbana no Spotify", "Procure a música X", "No Spotify, pesquise Y"
    for pattern in (_SPOT_A, _SPOT_B, _SPOT_C):
        m = pattern.match(rest)
        if m:
            start, end = m.start("q"), m.end("q")
            q = text[idx[base + start]: idx[base + end]] if base + end < len(idx) else original(start)
            q = q.strip(" \t\n.!?")
            nm = _SPOT_NOUN.match(re.sub(r"\s+", " ", " ".join(fold_with_map(q)[0].split())))
            if nm:  # tira "a música", "o artista" do início, preservando a grafia original do resto
                f, ix = fold_with_map(q)
                nm2 = _SPOT_NOUN.match(f)
                q = q[ix[nm2.end()]:] if nm2 and nm2.end() < len(ix) else ""
            return ActionParse("spotify", raw=q.strip(" \t\n.!?"))
    m = _CLOSE.match(rest)
    if m:
        noun, target, raw = _split_body(m.group("body"), original(m.start("body")), fold_with_map(original(m.start("body")))[1])
        return ActionParse("close", noun, target, raw)
    m = _OPEN.match(rest)
    if m:
        orig_body = original(m.start("body"))
        noun, target, raw = _split_body(m.group("body"), orig_body, fold_with_map(orig_body)[1])
        if not noun and re.match(r"(?:https?://|www\.)", raw, re.I):
            noun = "site"
        return ActionParse("open", noun, target, raw)
    return None


# --- confirmação por texto ou voz (só vale com uma confirmação pendente; ver ActionService) -----------------------------
_JARVIS_PREFIX = re.compile(r"^((ei |hey |oi )?jarvis )+")
_CONFIRM_TEXT = {"sim", "confirmo", "confirmar", "confirmado", "pode fechar", "sim pode fechar", "sim feche", "sim confirmo", "pode sim", "sim pode"}
_CONFIRM_VOICE = {"confirmo", "confirmar", "confirmado", "pode fechar", "sim pode fechar", "sim feche", "sim confirmo"}  # nada de "sim" solto: ruído/eco
_CANCEL = {"nao", "cancelar", "cancele", "cancela", "cancelado", "nao feche", "melhor nao", "deixa pra la", "deixa para la", "esquece", "esqueca",
           "nao precisa", "desisto", "nao confirmo", "nao pode"}


def classify_confirmation(text: str, source: str = "texto") -> str | None:
    """"confirm" | "cancel" | None. A frase inteira precisa ser uma resposta curta; por voz, "sim" sozinho NÃO confirma.
    Isto só é consultado quando existe uma confirmação pendente."""
    t = _JARVIS_PREFIX.sub("", _norm(text)).strip()
    if t in _CANCEL:
        return "cancel"
    if t in (_CONFIRM_VOICE if source == "voz" else _CONFIRM_TEXT):
        return "confirm"
    return None

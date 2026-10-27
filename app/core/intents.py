"""Roteador LOCAL e conservador: decide, sem IA e sem custo, quando a resposta já é conhecida.

Cobre só o que o aplicativo sabe que NÃO pode fazer ainda (ações no computador e serviços externos) e perguntas sobre
as próprias capacidades. Qualquer coisa ambígua ou informativa ("Como posso abrir o Spotify usando Python?") segue
para a IA, que recebe a lista real de capacidades no prompt. Nada aqui executa algo."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum, auto

from app.core.capabilities import PLANNED, Capability, CapabilityRegistry
from app.core.textutil import fold_with_map


class IntentKind(Enum):
    ACTION_UNAVAILABLE = auto()  # pedido de execução ("Abra o Spotify")
    EXTERNAL_UNAVAILABLE = auto()  # dado atual de serviço externo ("Qual a previsão do tempo?")
    CAPABILITY_QUESTION = auto()  # "Você já consegue abrir o Spotify?"
    CAPABILITIES_LIST = auto()  # "O que você consegue fazer?"


@dataclass(frozen=True)
class LocalIntent:
    kind: IntentKind
    capability: Capability | None = None


def _norm(text: str) -> str:
    d = unicodedata.normalize("NFD", text.lower())
    t = "".join(c for c in d if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", t)).strip()


_PREFIX = re.compile(r"^((ei |hey )?jarvis |por favor |pode |poderia |voce pode |voce poderia |eu quero que voce |quero que voce "
                     r"|preciso que voce |gostaria que voce |agora |entao )+")
_INFORMATIVE = re.compile(r"^(como |por que |porque |o que (e|significa|sao) |qual a diferenca|explique|me explique|ensine|me ensine|"
                          r"escreva|crie |faca um|gere |traduza|resuma|para que serve|me mostre|mostre |me diga como|diga como|me ajude|ajude )")
_ACTIONS = [  # (capacidade, verbos imperativos no início da frase)
    (Capability.OPEN_APPS, r"abra|abre|abrir|inicie|iniciar|lance|lancar"),
    (Capability.CLOSE_APPS, r"feche|fecha|fechar|encerre|encerrar"),
    (Capability.MEDIA_CONTROL, r"toque|toca|tocar|reproduza|coloque para tocar|pause|pausa|pausar|pule|aumente o volume|"
                               r"diminua o volume|abaixe o volume|suba o volume|silencie|mute"),
    (Capability.SYSTEM_CONTROL, r"desligue|reinicie|bloqueie|minimize|maximize|suspenda|hiberne"),
]
_EXTERNAL = [  # padrões específicos de dado ATUAL ou pessoal; "poema sobre o clima" não casa
    (Capability.WEATHER, r"\b(previsao do tempo|vai chover|vai fazer (frio|calor)|temperatura (agora|hoje|em |de amanha)|"
                         r"como (esta|ta) o (tempo|clima)|(clima|tempo) (hoje|agora|de hoje|de amanha|em ))"),
    (Capability.CALENDAR, r"\b(meus? compromissos?|minha agenda|que compromissos|proximo compromisso|meus eventos|"
                          r"tenho (alguma )?(reuniao|compromisso))"),
    (Capability.EMAIL, r"\b(meus? e ?mails?|caixa de entrada|novos e ?mails?)"),
    (Capability.NEWS, r"\b(noticias? (de hoje|do dia|recentes|de agora)|ultimas noticias|principais noticias|manchetes)"),
    (Capability.WEB_SEARCH, r"\b(cotacao|dolar hoje|placar|resultado do jogo|pesquise na (web|internet)|busque na (web|internet))"),
]
_CAN = re.compile(r"\b(voce|vc|jarvis)\b.*\b(ja )?(consegue|sabe|tem como|e capaz de)\b")
_LIST = re.compile(r"\b(o que|que coisas|quais coisas) (voce|vc) (consegue|pode|sabe) fazer\b|\b(suas|quais sao suas) (funcoes|capacidades)\b")


def classify(text: str) -> LocalIntent | None:
    t = _norm(text)
    if not t:
        return None
    if _LIST.search(t):
        return LocalIntent(IntentKind.CAPABILITIES_LIST)
    t = _PREFIX.sub("", t)
    if _INFORMATIVE.match(t):
        return None  # pergunta/pedido de conteúdo: é com a IA
    for cap, verbs in _ACTIONS:
        m = re.match(rf"({verbs})\b(?! (de|do|da|dos|das)\b)", t)
        if m:
            return LocalIntent(IntentKind.ACTION_UNAVAILABLE, cap)
    for cap, verbs in _ACTIONS:
        if _CAN.search(t):
            infinitive = {Capability.OPEN_APPS: r"abrir|abre", Capability.CLOSE_APPS: r"fechar|fecha", Capability.MEDIA_CONTROL: r"tocar|toca|pausar",
                          Capability.SYSTEM_CONTROL: r"desligar|reiniciar|bloquear"}[cap]
            if re.search(rf"\b({infinitive})\b", t):
                return LocalIntent(IntentKind.CAPABILITY_QUESTION, cap)
    for cap, pattern in _EXTERNAL:
        if re.search(pattern, t):
            return LocalIntent(IntentKind.EXTERNAL_UNAVAILABLE, cap)
    return None


_ACTION_REPLY = {
    Capability.OPEN_APPS: "abrir programas", Capability.CLOSE_APPS: "fechar programas",
    Capability.MEDIA_CONTROL: "controlar músicas ou o volume", Capability.SYSTEM_CONTROL: "controlar o computador",
}


def local_reply(intent: LocalIntent, registry: CapabilityRegistry) -> str:
    cap = intent.capability
    if intent.kind is IntentKind.CAPABILITIES_LIST:
        return (f"Hoje eu consigo: {', '.join(registry.available())}. "
                f"Ainda não consigo: {', '.join(registry.unavailable())}. Esses recursos virão nas próximas etapas.")
    if registry.is_available(cap):  # só acontece quando a capacidade existir de verdade
        return "Sim, esse recurso está disponível."
    planned = PLANNED.get(cap, "uma etapa futura")
    if intent.kind is IntentKind.ACTION_UNAVAILABLE:
        return (f"Ainda não consigo {_ACTION_REPLY[cap]} diretamente. Esse recurso será implementado na {planned}. "
                "Não executei nenhuma ação.")
    if intent.kind is IntentKind.CAPABILITY_QUESTION:
        return f"Ainda não. Eu ainda não consigo {_ACTION_REPLY[cap]}; isso virá na {planned}."
    return (f"Ainda não tenho acesso a {cap.value} atualizada, então não vou inventar uma resposta. "
            f"Essa integração será adicionada na {planned}.")


# --- Comandos locais de memória (Etapa 6) -------------------------------------------------------------------------
# Determinístico e conservador: só reconhece pedidos IMPERATIVOS e claros no início da frase. "Lembre que ..." grava;
# "Como lembrar de X?" ou "Guarde o arquivo" seguem para a IA. Nada aqui grava: o MemoryService executa.
class MemoryKind(Enum):
    CREATE = auto()
    LIST = auto()
    SEARCH = auto()
    UPDATE = auto()
    DELETE = auto()


@dataclass(frozen=True)
class MemoryCommand:
    kind: MemoryKind
    text: str = ""  # CREATE: conteúdo (com a grafia original); SEARCH: consulta; UPDATE/DELETE: alvo (texto sem acento ou #código)
    new_text: str = ""  # UPDATE: novo conteúdo (grafia original)
    force: bool = False  # CREATE com "também"/"mesmo assim": não pergunta sobre memória parecida


_M_PREFIX = re.compile(r"^(?:[\s,.:;!\-]+|(?:ei|hey|oi|ola)\s+jarvis\b|jarvis\b|por favor\b|pode\b|poderia\b|voce\s+pode\b|voce\s+poderia\b|"
                       r"(?:eu\s+)?quero\s+que\s+voce\b|preciso\s+que\s+voce\b|gostaria\s+que\s+voce\b|agora\b|entao\b)*")
_M_CREATE = re.compile(
    r"^(?:(?:lembre|lembra|lembrar|memorize|memorizar)(?:-se)?|"
    r"(?:guarde|guarda|guardar|anote|anota|anotar|registre|registrar|salve|salvar|adicione|adicionar)"
    r"(?:\s+(?:isso|essa informacao|a informacao))?(?:\s+(?:na|em)\s+(?:minha\s+)?memoria)?(?:\s+para\s+lembrar)?)"
    r"(?P<force>\s+(?:tambem|mesmo assim))?(?:\s+de\s+que\b|\s+que\b|\s*:)\s*(?P<body>\S.*)$", re.S)
_M_LIST = re.compile(r"^(?:o que voce (?:lembra|sabe) (?:de|sobre|a respeito de) mim|"
                     r"(?:liste|lista|listar|mostre|mostra|mostrar|me mostre|exiba|quais sao|quais|me diga) (?:todas )?(?:as )?(?:minhas )?memorias|"
                     r"minhas memorias|que memorias (?:voce )?(?:tem|guardou))[\s?.!]*$")
_M_UPDATE = re.compile(r"^(?:altere|alterar|atualize|atualizar|edite|editar|corrija|corrigir|mude|mudar|modifique|modificar)\s+"
                       r"(?:a |essa )?(?:minha )?memoria\s+(?:(?:sobre|de|do|da)\s+)?(?P<target>.+?)\s*(?:\s(?:para|por|como)\s|:)\s*(?P<new>\S.*)$", re.S)
_M_DELETE = re.compile(r"^(?:(?:esqueca|esquecer|apague|apagar|exclua|excluir|remova|remover|delete|deletar)\s+(?:(?:a|essa|minha)\s+)*memoria"
                       r"|(?:esqueca|esquecer)\s+(?:que|isso:))\s*(?:(?:sobre|de|do|da|que)\s+)?(?P<target>\S.*)$", re.S)
_M_DELETE_ALL = re.compile(r"^(?:esqueca|esquecer|apague|apagar|exclua|excluir|remova|remover|delete|deletar|limpe|limpar)\s+(?:todas\s+(?:as\s+)?(?:minhas\s+)?memorias|"
                           r"(?:as\s+)?(?:minhas\s+)?memorias)[\s.!]*$")
_M_SEARCH = re.compile(r"^(?:(?:pesquise|pesquisa|pesquisar|busque|busca|buscar|procure|procurar|consulte|consultar)\s+(?:(?:nas|em|na|a|as)\s+)?"
                       r"(?:minhas\s+)?memorias?(?:\s+(?:sobre|de|do|da|por))?\s+(?P<q>\S.*)|"
                       r"o que voce lembra (?:sobre|de|do|da|a respeito de)\s+(?P<q2>\S.*))$", re.S)


def classify_memory(text: str) -> MemoryCommand | None:
    folded, idx = fold_with_map(text or "")
    base = _M_PREFIX.match(folded).end()
    rest = folded[base:]
    if not rest.strip():
        return None

    def original(pos: int) -> str:  # trecho do texto ORIGINAL (com acentos e maiúsculas) a partir de `pos` em `rest`
        return text[idx[base + pos]:].strip() if base + pos < len(idx) else ""

    def clean(t: str) -> str:
        return t.strip(" \t\n.?!")

    if m := _M_CREATE.match(rest):
        body = original(m.start("body"))
        return MemoryCommand(MemoryKind.CREATE, body, force=bool(m.group("force"))) if body else None
    if _M_LIST.match(rest):
        return MemoryCommand(MemoryKind.LIST)
    if m := _M_UPDATE.match(rest):
        return MemoryCommand(MemoryKind.UPDATE, clean(m.group("target")), original(m.start("new")))
    if _M_DELETE_ALL.match(rest):
        return MemoryCommand(MemoryKind.DELETE, "todas")  # o serviço recusa a exclusão em massa e explica
    if m := _M_DELETE.match(rest):
        return MemoryCommand(MemoryKind.DELETE, clean(m.group("target")))
    if m := _M_SEARCH.match(rest):
        return MemoryCommand(MemoryKind.SEARCH, clean(m.group("q") or m.group("q2") or ""))
    return None

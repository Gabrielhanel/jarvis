"""Reconhece, na mensagem do USUÁRIO, consultas a agenda, e-mails, notícias, clima e ao Resumo da Manhã. Determinístico, sem IA.
Só frases de consulta no início ("Quais são meus compromissos de hoje?"). Nada aqui executa ou consulta: só devolve a intenção."""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum, auto

from app.core.intents import _M_PREFIX
from app.core.textutil import fold


class IntegrationKind(Enum):
    AGENDA = auto()
    EMAIL = auto()
    NEWS = auto()
    WEATHER = auto()
    MORNING = auto()


@dataclass(frozen=True)
class IntegrationCommand:
    kind: IntegrationKind
    period: str = ""  # AGENDA: today | tomorrow | week
    mode: str = ""  # EMAIL: check | attention | info | noise
    topic: str = ""  # NEWS: assunto pedido (sem acento), vazio = principais
    when: str = ""  # WEATHER: now | today | tomorrow
    rain: bool = False  # WEATHER: a pergunta é sobre chuva
    detail: bool = False  # NEWS: "resuma"


_CUE = (r"(?:quais?(?: sao| serao)?|qual(?: e| sera)?|o que|oque|mostr\w+|me mostr\w+|exib\w+|ve(?:ja|r)?|consult\w+|diga|me diga|"
        r"leia|le|tenho|tem|ha|existem|resum\w+|como (?:esta|ta|vai estar|estara|fica|vai ficar))")
_AGENDA = re.compile(rf"^(?:{_CUE}\b.*\b(?:agenda|compromissos?|reunioes|reuniao|eventos?)\b|(?:minha agenda|agenda|meus compromissos|compromissos)\b)")
_AGENDA_SCOPE = re.compile(r"\b(meu|meus|minha|minhas|tenho|hoje|amanha|semana)\b")
_EMAIL_NOUN = re.compile(r"\b(?:e-?mails?|gmail|caixa de entrada)\b")
_EMAIL_CUE = re.compile(r"^(?:verifi\w+|confir\w+|checa\w*|cheque|ve(?:ja|r)?|mostr\w+|me mostr\w+|liste|lista|quais?(?: sao)?|tem|tenho|ha|existem|resum\w+|atualiz\w+|olha|olhe|leia|ler)\b")
_NEWS_NOUN = re.compile(r"\b(?:noticias|manchetes)\b")  # só plural: "me conte uma notícia engraçada" é conversa, não o radar
_NEWS_CUE = re.compile(r"^(?:(?:as |os )?(?:principais |ultimas )?(?:noticias|manchetes)|quais?(?: sao)?|qual(?: e)?|mostr\w+|me mostr\w+|resum\w+|me conte|conte|diga|me diga|leia|ha|tem|me d[aeê]|principais|ultimas)\b")
_NEWS_TOPIC = re.compile(r"\b(?:noticias|manchetes)\s+(?:(?:de|do|da|sobre|em|no|na)\s+)?(?P<t>[a-z0-9][a-z0-9 ]*?)\s*[?.!]*$")
_NOT_TOPICS = {"hoje", "agora", "ontem", "hoje em dia", "dia", "manha", "tarde", "noite", "ultima hora", "principais", "ultimas", "do dia", "de hoje", "de agora", "do mundo todo"}
_WEATHER = re.compile(
    r"^(?:como (?:esta|ta|vai estar|estara|fica|vai ficar) (?:o )?(?:tempo|clima)\b|(?:qual|quais)(?: e| sera| sao)? (?:a |as )?(?:previsao|temperatura|o clima)\b|"
    r"previsao (?:do tempo|para|de|da)\b|previsao$|(?:vai|ira|vai ter|tera|tem) (?:chover|chuva)\b|(?:esta|ta) chovendo\b|(?:faz|vai fazer|vai estar|esta|ta) (?:frio|calor)\b|"
    r"temperatura (?:agora|hoje|amanha)\b|(?:vai|ira) (?:fazer )?(?:frio|calor)\b)")
_MORNING = re.compile(r"^(?:bom dia(?: jarvis)?|(?:faca|faz|me de|me fale|me conte|quero|mostre|mostra|gere|prepare)\s+(?:o |meu |um )*(?:resumo|briefing)(?: da manha| do dia)?|(?:meu )?resumo(?: da manha| do dia)?)[\s,.!?]*$")


def classify_integration(text: str) -> IntegrationCommand | None:
    folded = fold(text or "")
    rest = folded[_M_PREFIX.match(folded).end():].strip()
    if not rest:
        return None
    if _MORNING.match(rest):
        return IntegrationCommand(IntegrationKind.MORNING)
    if _WEATHER.match(rest):
        rain = bool(re.search(r"\b(?:chover|chuva|chovendo)\b", rest))
        when = "tomorrow" if "amanha" in rest else "now" if re.search(r"\b(agora|chovendo|esta|ta)\b", rest) and "hoje" not in rest else "today"
        return IntegrationCommand(IntegrationKind.WEATHER, when=when, rain=rain)
    if _EMAIL_NOUN.search(rest) and _EMAIL_CUE.match(rest) and not re.match(r"^tenho (?:que|de)\b", rest):
        if re.search(r"atencao|precisam|precisa|importante|urgente|\bacao\b|responder|prioridade", rest):
            mode = "attention"
        elif re.search(r"informativ|\binfo\b", rest):
            mode = "info"
        elif re.search(r"ruido|spam|promo|publicidade|propaganda|lixo", rest):
            mode = "noise"
        else:
            mode = "check"
        return IntegrationCommand(IntegrationKind.EMAIL, mode=mode)
    if _NEWS_NOUN.search(rest) and _NEWS_CUE.match(rest):
        m = _NEWS_TOPIC.search(rest)
        topic = m.group("t").strip() if m and m.group("t").strip() not in _NOT_TOPICS else ""
        return IntegrationCommand(IntegrationKind.NEWS, topic=topic, detail=bool(re.match(r"^resum", rest)))
    if _AGENDA.match(rest) and _AGENDA_SCOPE.search(rest):
        period = "week" if "semana" in rest else "tomorrow" if "amanha" in rest else "today"
        return IntegrationCommand(IntegrationKind.AGENDA, period=period)
    return None

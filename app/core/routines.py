"""Rotinas (Etapa 9): sequências de ações que o Jarvis JÁ sabe fazer. Cada etapa é uma ação ESTRUTURADA de uma lista fixa:
não existe campo de script, comando, shell ou código. Os valores são validados aqui ao salvar e REVALIDADOS pelo ActionService /
IntegrationManager na hora de executar (a rotina não concede permissão nenhuma)."""
from __future__ import annotations

import json
import re
import unicodedata
import uuid
from dataclasses import dataclass, field

from app.core.actions import build_spotify_url, validate_url
from app.core.textutil import norm_alias

MAX_STEPS = 10
_NAME = re.compile(r"[\w .'+\-]{1,60}")  # nomes de destino: sem barras, dois-pontos, ponto e vírgula, aspas ou outros símbolos de comando
_TOPIC = re.compile(r"[\w ]{0,40}")


@dataclass(frozen=True)
class StepType:
    key: str
    label: str
    options: tuple[str, ...] = ()  # valores permitidos (vazio = texto validado à parte)
    option_labels: tuple[str, ...] = ()
    needs_value: bool = True
    confirms: bool = False  # sempre exige a confirmação da Etapa 7


STEP_TYPES: dict[str, StepType] = {s.key: s for s in (
    StepType("agenda", "Consultar agenda", ("today", "tomorrow", "week"), ("hoje", "amanhã", "semana")),
    StepType("email", "Verificar e-mails", ("check", "attention", "info"), ("resumo", "precisam de atenção", "informativos")),
    StepType("news", "Consultar notícias"),  # valor opcional: assunto
    StepType("weather", "Consultar o clima", ("now", "today", "tomorrow"), ("agora", "hoje", "amanhã")),
    StepType("morning", "Resumo da manhã", needs_value=False),
    StepType("open_app", "Abrir aplicativo"),
    StepType("open_folder", "Abrir pasta"),
    StepType("open_project", "Abrir projeto"),
    StepType("open_site", "Abrir site (nome ou https://…)"),
    StepType("spotify_search", "Pesquisar no Spotify"),
    StepType("close_app", "Fechar aplicativo (pede confirmação)", confirms=True),
)}


@dataclass(frozen=True)
class RoutineStep:
    type: str
    value: str = ""
    essential: bool = True  # se falhar, a rotina para; senão segue para a próxima etapa

    def to_dict(self) -> dict:
        return {"type": self.type, "value": self.value, "essential": self.essential}


@dataclass(frozen=True)
class Routine:
    id: str
    name: str
    steps: tuple[RoutineStep, ...]
    enabled: bool = True
    trigger: str = ""  # frase opcional que dispara a rotina por voz/texto (a frase INTEIRA)
    valid: bool = True  # False = os dados salvos estavam corrompidos; não executa


def new_id() -> str:
    return uuid.uuid4().hex


def name_key(name: str) -> str:
    return norm_alias(name) or name.strip().lower()


def validate_name(name: str) -> tuple[str, str | None]:
    text = " ".join("".join(" " if unicodedata.category(c) in ("Cc", "Cf") else c for c in (name or "")).split())
    if not text or not any(c.isalnum() for c in text):
        return "", "Dê um nome à rotina (1 a 60 caracteres, com letras ou números)."
    if not _NAME.fullmatch(text):
        return "", "O nome só pode ter letras, números, espaços e - _ . ' +"
    return text, None


def validate_trigger(text: str) -> tuple[str, str | None]:
    t = " ".join((text or "").split())
    if not t:
        return "", None
    if len(t) > 80 or not re.fullmatch(r"[\w .,!?'+\-]{1,80}", t) or not any(c.isalnum() for c in t):
        return "", "A frase de voz deve ter até 80 caracteres, com letras ou números e pontuação simples."
    return t, None


def validate_step(raw: dict, index: int) -> tuple[RoutineStep | None, str | None]:
    where = f"Etapa {index}"
    kind, value = (raw.get("type") if isinstance(raw, dict) else None), (raw.get("value", "") if isinstance(raw, dict) else "")
    spec = STEP_TYPES.get(kind) if isinstance(kind, str) else None
    if spec is None:
        return None, f"{where}: ação não permitida."
    value = " ".join(str(value or "").split())
    if not spec.needs_value:
        value = ""
    elif spec.options:
        if value not in spec.options:
            return None, f"{where} ({spec.label}): escolha uma das opções."
    elif kind == "news":
        if not _TOPIC.fullmatch(value):
            return None, f"{where}: o assunto aceita só letras, números e espaços (até 40)."
    elif kind == "spotify_search":
        if build_spotify_url(value)[1]:
            return None, f"{where}: texto de pesquisa inválido (1 a 100 caracteres, sem segredos)."
    elif kind == "open_site" and re.match(r"https?://|www\.", value, re.I):
        url, why = validate_url(value if value.lower().startswith("http") else "https://" + value)
        if why:
            return None, f"{where}: endereço inválido (só http/https, sem usuário/senha)."
        value = url
    elif not _NAME.fullmatch(value) or not any(c.isalnum() for c in value):
        return None, f"{where} ({spec.label}): informe só o NOME do destino cadastrado (sem caminhos nem símbolos)."
    essential = bool(raw.get("essential", True)) if isinstance(raw, dict) else True
    return RoutineStep(kind, value, True if spec.confirms else essential), None


def validate_steps(raw_steps) -> tuple[tuple[RoutineStep, ...], list[str]]:
    if not isinstance(raw_steps, (list, tuple)) or not raw_steps:
        return (), ["Adicione pelo menos uma etapa."]
    if len(raw_steps) > MAX_STEPS:
        return (), [f"No máximo {MAX_STEPS} etapas por rotina."]
    steps, errors = [], []
    for i, raw in enumerate(raw_steps, 1):
        step, err = validate_step(raw, i)
        (errors.append(err) if err else steps.append(step))
    return (tuple(steps), errors)


def steps_to_json(steps) -> str:
    return json.dumps([s.to_dict() for s in steps], ensure_ascii=False)


def steps_from_json(text: str) -> tuple[tuple[RoutineStep, ...], bool]:
    """(etapas, válido). Dados salvos que não passam na validação NÃO viram uma rotina executável."""
    try:
        raw = json.loads(text)
    except (TypeError, ValueError):
        return (), False
    steps, errors = validate_steps(raw)
    return steps, not errors


def describe_step(step: RoutineStep) -> str:
    spec = STEP_TYPES[step.type]
    if spec.options:
        return f"{spec.label}: {spec.option_labels[spec.options.index(step.value)]}"
    return f"{spec.label}: {step.value}" if step.value else spec.label

"""Esquema das preferências do usuário (Etapa 9). Cada preferência tem UMA chave no settings.json já existente (nada é duplicado),
um valor padrão seguro e uma validação. Valor ausente ou inválido volta ao padrão sem derrubar nada.
Segredos (chave da API, senhas, endereços privados) NÃO passam por aqui: seguem no .env/cofre da Etapa 8."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.config import AIConfig, TTSConfig, VoiceConfig
from app.core.modes import MODES, DEFAULT_MODE

SUPPORTED_MODELS = ("claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5")  # só modelos já suportados pelo cliente existente
TTS_VOICES = ("pm_alex", "pm_santa", "pf_dora")
PERIOD_CHOICES = ("today", "7d", "30d", "all")


@dataclass(frozen=True)
class Setting:
    key: str  # caminho no settings.json
    kind: str  # choice | bool | int | float
    default: Any
    section: str
    label: str
    choices: tuple = ()
    low: float | None = None
    high: float | None = None
    restart: bool = False  # só vale na próxima abertura do Jarvis
    help: str = ""


_A, _V, _I, _S, _R, _M = "Assistente e IA", "Voz", "Interface e histórico", "Atalho global", "Rotinas", "Métricas"
SETTINGS: dict[str, Setting] = {s.key: s for s in (
    Setting("mode", "choice", DEFAULT_MODE, _A, "Modo de funcionamento", tuple(MODES), help="Cada modo muda limites e o uso da IA; não troca modelo nem voz."),
    Setting("ai.model", "choice", AIConfig().model, _A, "Modelo da IA", SUPPORTED_MODELS, help="É o modelo realmente enviado à API."),
    Setting("ai.max_output_tokens", "int", AIConfig().max_output_tokens, _A, "Tamanho máximo da resposta (tokens)", low=100, high=2000),
    Setting("ai.speak_responses", "bool", AIConfig().speak_responses, _A, "Falar as respostas da IA"),
    Setting("tts.enabled", "bool", TTSConfig().enabled, _V, "Voz do Jarvis ativada", restart=True),
    Setting("tts.voice", "choice", TTSConfig().voice, _V, "Voz", TTS_VOICES, restart=True),
    Setting("tts.volume", "float", TTSConfig().volume, _V, "Volume da fala", low=0.0, high=1.0),
    Setting("tts.rate", "float", TTSConfig().rate, _V, "Velocidade da fala", low=0.5, high=1.5),
    Setting("listening.enabled", "bool", VoiceConfig().enabled, _V, "Microfone e palavra de ativação", restart=True, help="Desligado: o Jarvis não escuta, nem a palavra de ativação."),
    Setting("listening.wake_allow_name_only", "bool", VoiceConfig().wake_allow_name_only, _V, "Aceitar só “Jarvis” (sem “Ei”)", restart=True),
    Setting("ui.sidebar_visible", "bool", True, _I, "Mostrar a lista de conversas"),
    Setting("history.restore_on_start", "bool", True, _I, "Reabrir a última conversa ao iniciar", restart=True),
    Setting("shortcut.enabled", "bool", True, _S, "Atalho global ativado"),
    Setting("shortcut.combo", "str", "Ctrl+Alt+J", _S, "Combinação de teclas", help="Precisa de pelo menos 2 modificadores (ou 1 + tecla F). Ex.: Ctrl+Alt+J."),
    Setting("routines.confirm_before_run", "bool", False, _R, "Perguntar antes de executar uma rotina pelo botão"),
    Setting("metrics.default_period", "choice", "7d", _M, "Período padrão das métricas", PERIOD_CHOICES),
)}


def coerce(setting: Setting, raw: Any) -> tuple[Any, str | None]:
    """(valor validado, None) ou (None, mensagem). Não aceita "quase certo": o tipo e a faixa precisam estar corretos."""
    k = setting.kind
    if k == "bool":
        return (raw, None) if isinstance(raw, bool) else (None, "Valor deve ser ligado ou desligado.")
    if k == "choice":
        return (raw, None) if raw in setting.choices else (None, "Opção desconhecida.")
    if k == "int":
        if isinstance(raw, bool) or not isinstance(raw, int) or not (setting.low <= raw <= setting.high):
            return None, f"Use um número inteiro entre {int(setting.low)} e {int(setting.high)}."
        return raw, None
    if k == "float":
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not (setting.low <= float(raw) <= setting.high):
            return None, f"Use um número entre {setting.low} e {setting.high}."
        return round(float(raw), 2), None
    if k == "str":
        return (raw.strip(), None) if isinstance(raw, str) and 1 <= len(raw.strip()) <= 60 else (None, "Texto inválido.")
    return None, "Tipo desconhecido."

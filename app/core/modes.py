"""Modos de funcionamento (Etapa 9): perfis pequenos que mudam COMPORTAMENTO real, sem criar outro pipeline.

O modo fica em `config["mode"]` (a única fonte, a mesma que o cabeçalho já mostrava). Trocar de modo NUNCA altera modelo, voz,
credenciais nem escolhas de segurança: aplica apenas o que está descrito aqui."""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass, replace

from app.core.config import AIConfig


@dataclass(frozen=True)
class ModeProfile:
    key: str
    label: str
    description: str
    ai_chat: bool  # conversa livre pode ir à IA
    ai_extras: bool  # classificação de e-mails e reescrita do resumo por IA
    network: bool  # integrações podem consultar a internet
    max_output_tokens: int | None = None  # tetos (só REDUZEM o que o usuário já configurou)
    context_token_budget: int | None = None
    max_history_messages: int | None = None


MODES: dict[str, ModeProfile] = {
    "EQUILIBRADO": ModeProfile("EQUILIBRADO", "Equilibrado", "Comportamento padrão: comandos locais primeiro; a IA responde à conversa; integrações atualizam sozinhas.", True, True, True),
    "ECONOMICO": ModeProfile("ECONOMICO", "Econômico", "Menos tokens: respostas até 250 tokens, contexto menor (6 mensagens) e sem IA em tarefas extras (e-mails, resumo). A conversa ainda usa a IA.",
                             True, False, True, 250, 1500, 6),
    "LOCAL": ModeProfile("LOCAL", "Local", "Nenhuma mensagem vai à IA: só comandos locais, memória, rotinas, agenda, e-mails, notícias e clima. Integrações ainda usam a internet.", False, False, True),
    "OFFLINE": ModeProfile("OFFLINE", "Offline", "Sem internet no atendimento: nada vai à IA e as integrações mostram só os dados salvos. Voz, memória, rotinas e ações locais continuam.", False, False, False),
}
DEFAULT_MODE = "EQUILIBRADO"


def normalize_mode(value: object) -> str:
    """Chave válida para qualquer valor salvo (sem acento, maiúsculas); o que for desconhecido volta ao padrão."""
    text = "".join(c for c in unicodedata.normalize("NFD", str(value or "")) if unicodedata.category(c) != "Mn").strip().upper()
    return text if text in MODES else DEFAULT_MODE


def profile(value: object) -> ModeProfile:
    return MODES[normalize_mode(value)]


def effective_ai_config(base: AIConfig, mode: object) -> AIConfig:
    """Configuração de IA efetiva: a do usuário, com os tetos do modo. Nunca troca modelo, esforço nem timeouts."""
    p = profile(mode)
    changes = {}
    for name in ("max_output_tokens", "context_token_budget", "max_history_messages"):
        cap = getattr(p, name)
        if cap is not None:
            changes[name] = min(getattr(base, name), cap)
    return replace(base, **changes) if changes else base


def ai_allowed_by_mode(mode: object) -> bool:
    """Tarefas EXTRAS de IA (classificar e-mails, reescrever o resumo) só no modo Equilibrado."""
    return profile(mode).ai_extras

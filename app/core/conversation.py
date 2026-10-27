"""Contexto temporário da conversa atual, com orçamento de tokens. Testável e independente de UI e de rede."""
from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass
from typing import Iterable

from app.core.config import AIConfig

log = logging.getLogger("jarvis.ai")


def estimate_tokens(text: str, chars_per_token: float = 2.5) -> int:
    """ESTIMATIVA conservadora (não é contagem real; o valor exato vem da API na resposta)."""
    return math.ceil(len(text) / chars_per_token) + 4


@dataclass(frozen=True)
class Turn:
    user: str
    assistant: str


@dataclass(frozen=True)
class BuiltContext:
    messages: list[dict]  # formato da API: alterna user/assistant, começa e termina com "user"
    estimated_input_tokens: int
    dropped_turns: int
    over_budget: bool  # sistema + mensagem atual sozinhos já passam do orçamento (nada é cortado deles)


class ConversationContext:
    def __init__(self, cfg: AIConfig) -> None:
        self._cfg = cfg
        self._turns: list[Turn] = []
        self._lock = threading.Lock()  # a thread de trabalho monta o contexto enquanto a UI pode trocar a conversa

    def __len__(self) -> int:
        return len(self._turns)

    def clear(self) -> None:
        with self._lock:
            self._turns.clear()

    def set_config(self, cfg: AIConfig) -> None:
        """Novos limites (troca de modo): valem a partir da próxima montagem de contexto."""
        self._cfg = cfg

    def add_exchange(self, user: str, assistant: str) -> None:
        """Só trocas COMPLETAS entram no histórico (a mensagem atual é passada à parte em build())."""
        with self._lock:
            self._turns.append(Turn(user, assistant))

    def load_turns(self, turns: Iterable[tuple[str, str]]) -> None:
        """Substitui o contexto por trocas completas (user, assistant) lidas do histórico persistente, da mais antiga à mais nova.
        O orçamento e o limite de mensagens continuam sendo aplicados em build(); aqui nada é cortado."""
        loaded = [Turn(u, a) for u, a in turns]
        with self._lock:
            self._turns = loaded

    def build(self, current: str, system: str) -> BuiltContext:
        with self._lock:
            turns = list(self._turns)
        est = lambda t: estimate_tokens(t, self._cfg.chars_per_token)
        fixed = est(system) + est(current)
        remaining, kept = self._cfg.context_token_budget - fixed, []
        for turn in reversed(turns):  # do mais recente para o mais antigo; turnos inteiros, nunca cortados no meio
            cost = est(turn.user) + est(turn.assistant)
            if (len(kept) + 1) * 2 + 1 > self._cfg.max_history_messages or cost > remaining:
                break
            kept.append(turn)
            remaining -= cost
        kept.reverse()
        messages = [m for t in kept for m in ({"role": "user", "content": t.user}, {"role": "assistant", "content": t.assistant})]
        messages.append({"role": "user", "content": current})
        used = fixed + sum(est(m["content"]) for m in messages[:-1])
        over = fixed > self._cfg.context_token_budget
        if over:
            log.warning("[AI] Sistema + mensagem atual passam do orçamento de contexto (estimado %d); histórico omitido", fixed)
        return BuiltContext(messages, used, len(turns) - len(kept), over)

"""Confirmações pendentes: UMA por vez, com ID único, prazo e uso único. Seguro entre threads; relógio injetável nos testes.

- Uma nova solicitação SUBSTITUI a pendente: a confirmação antiga nunca autoriza a ação nova (IDs diferentes).
- Confirmar consome a solicitação; confirmar de novo (ou depois do prazo) não executa nada.
- Nada aqui executa a ação: devolve a solicitação para o ActionService revalidar e executar."""
from __future__ import annotations

import threading
import time
import uuid
from typing import Callable

from app.core.actions import ActionRequest, PendingConfirmation


class ConfirmationManager:
    EXPIRED_MEMORY_S = 60.0  # por quanto tempo lembramos que uma confirmação expirou (para responder "expirou" em vez de ignorar)

    def __init__(self, ttl_s: float = 30.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl_s, self._clock = ttl_s, clock
        self._lock = threading.Lock()
        self._pending: PendingConfirmation | None = None
        self._expired: tuple[str, float] | None = None  # (id, quando expirou)

    def create(self, request: ActionRequest, question: str, summary: str) -> tuple[PendingConfirmation, str | None]:
        """(nova pendente, id da pendente substituída ou None)."""
        with self._lock:
            old = self._current_locked()
            pending = PendingConfirmation(uuid.uuid4().hex, request, question, summary, self.ttl_s, self._clock() + self.ttl_s)
            self._pending = pending
            return pending, (old.id if old else None)

    def current(self) -> PendingConfirmation | None:
        with self._lock:
            return self._current_locked()

    def _current_locked(self) -> PendingConfirmation | None:
        p = self._pending
        if p is not None and self._clock() >= p.expires_at:
            self._pending, self._expired = None, (p.id, p.expires_at)  # quando VENCEU, não quando alguém notou
            return None
        return p

    def take(self, pending_id: str) -> tuple[str, PendingConfirmation | None]:
        """Consome a confirmação. ("ok", pendente) | ("expired", None) | ("none", None): nada pendente, ID antigo ou já usada."""
        with self._lock:
            p = self._current_locked()
            if p is None:
                if self._expired and self._expired[0] == pending_id:
                    self._expired = None
                    return "expired", None
                return "none", None
            if p.id != pending_id:
                return "none", None  # ID de outra solicitação: não autoriza esta
            self._pending = None
            return "ok", p

    def cancel(self, pending_id: str | None = None) -> str | None:
        """Cancela a pendente (se `pending_id` for dado, só se for ela). Devolve o ID cancelado ou None."""
        with self._lock:
            p = self._current_locked()
            if p is None or (pending_id is not None and p.id != pending_id):
                return None
            self._pending = None
            return p.id

    def recently_expired(self, consume: bool = True) -> bool:
        """Uma confirmação expirou há pouco e ainda não foi explicada ao usuário? Por padrão consome o aviso."""
        with self._lock:
            self._current_locked()
            if self._expired and self._clock() - self._expired[1] <= self.EXPIRED_MEMORY_S:
                if consume:
                    self._expired = None
                return True
            return False

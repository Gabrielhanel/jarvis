"""Histórico persistente: conversas e mensagens. Sem UI e sem rede. Seguro entre threads (cada operação usa a própria conexão).

A conversa ATIVA é lembrada em memória (lida uma vez na inicialização) para que `submit()` não precise ler o banco.
Nesta etapa só existe uma conversa em uso, mas tudo é por `conversation_id`, pronto para várias sessões (Etapa 9)."""
from __future__ import annotations

import logging
import threading
import unicodedata

from app.database.database import DatabaseError
from app.database.repositories import ConversationRepository, StoredMessage

log = logging.getLogger("jarvis.history")


def clean_title(text: str, max_chars: int = 60) -> str | None:
    """Título válido ou None: sem caracteres de controle, espaços normalizados, até `max_chars` e com pelo menos uma letra/número."""
    line = " ".join("".join(" " if unicodedata.category(c) in ("Cc", "Cf") else c for c in (text or "")).split())
    return line if line and len(line) <= max_chars and any(c.isalnum() for c in line) else None


def make_title(text: str, max_chars: int = 60) -> str:
    """Título local (sem IA) a partir da primeira mensagem; nunca vazio nem feito só de símbolos."""
    line = " ".join((text or "").split())
    if not any(c.isalnum() for c in line):
        return ConversationRepository.DEFAULT_TITLE
    if len(line) <= max_chars:
        return line
    cut = line[:max_chars].rsplit(" ", 1)[0] or line[:max_chars]
    return cut.rstrip(" ,.;:") + "…"


class HistoryService:
    def __init__(self, repo: ConversationRepository, title_max_chars: int = 60) -> None:
        self.repo, self._title_max = repo, title_max_chars
        self._lock = threading.Lock()
        self._active_id: str | None = None

    @property
    def active_id(self) -> str | None:
        with self._lock:
            return self._active_id

    # --- conversas ---
    def ensure_active(self) -> str:
        """Recupera a conversa ativa; senão reativa a mais recente; senão cria a primeira. Não cria uma conversa nova a cada início."""
        conv = self.repo.get_active()
        if conv is None:
            latest = self.repo.latest()
            if latest is not None:
                self.repo.set_active(latest.id)
                conv = latest
        cid = conv.id if conv is not None else self.repo.create()
        with self._lock:
            self._active_id = cid
        return cid

    def create_conversation(self, title: str | None = None, activate: bool = True) -> str:
        cid = self.repo.create(title, activate)
        if activate:
            with self._lock:
                self._active_id = cid
        return cid

    def set_active(self, conversation_id: str) -> None:
        self.repo.set_active(conversation_id)
        with self._lock:
            self._active_id = conversation_id

    def get_conversation(self, conversation_id: str):
        return self.repo.get(conversation_id)

    # --- gerenciamento visual (Etapa 9) ---
    def list_conversations(self, limit: int = 200):
        return self.repo.list(limit)

    def new_conversation(self) -> tuple[str, bool]:
        """(id, criada_agora). Se a conversa ativa ainda está VAZIA, ela mesma é a "nova": não acumula conversas vazias."""
        cid = self.active_id
        if cid is not None and self.repo.message_count(cid) == 0:
            return cid, False
        return self.create_conversation(activate=True), True

    def rename_conversation(self, conversation_id: str, title: str) -> tuple[bool, str]:
        cleaned = clean_title(title)
        if cleaned is None:
            return False, "Título inválido: use de 1 a 60 caracteres, com pelo menos uma letra ou número."
        return (True, cleaned) if self.repo.rename(conversation_id, cleaned) else (False, "Essa conversa não existe mais.")

    def delete_conversation(self, conversation_id: str) -> str:
        """Exclui só a conversa escolhida e devolve o id da conversa ativa depois disso (outra existente, ou uma nova e vazia)."""
        was_active = conversation_id == self.active_id
        if not self.repo.delete(conversation_id):
            raise DatabaseError("Essa conversa não existe mais.")
        if not was_active:
            return self.active_id or self.ensure_active()
        with self._lock:
            self._active_id = None
        return self.ensure_active()  # reativa a mais recente que sobrou ou cria uma nova

    # --- mensagens ---
    def record_user(self, conversation_id: str, submission_id: str, text: str, source: str = "texto") -> bool:
        """True = registrada agora; False = esta submissão já estava registrada (duplicada, nada foi gravado)."""
        return self.repo.insert_user(conversation_id, submission_id, text, source, make_title(text, self._title_max))

    def record_response(self, conversation_id: str, submission_id: str, text: str, origin: str, meta: dict | None = None,
                        attempts: int = 1) -> bool:
        meta = meta or {}
        return self.repo.insert_response(conversation_id, submission_id, text, origin, meta.get("model"),
                                         meta.get("input_tokens"), meta.get("output_tokens"), meta.get("duration_s"), attempts,
                                         bool(meta.get("private")))

    def mark_state(self, submission_id: str, state: str, error_kind: str | None = None, attempts: int | None = None,
                   only_from: tuple[str, ...] | None = None) -> bool:
        return self.repo.set_state(submission_id, state, error_kind, attempts, only_from)

    def mark_interrupted_on_start(self) -> int:
        n = self.repo.mark_interrupted()
        if n:
            log.warning("[HIST] %d mensagem(ns) de uma execução anterior ficaram sem resposta (marcadas como interrompidas; nada foi reenviado)", n)
        return n

    def load_messages(self, conversation_id: str, limit: int | None = None) -> list[StoredMessage]:
        return self.repo.messages(conversation_id, limit)

    def completed_turns(self, conversation_id: str, limit_turns: int) -> list[tuple[str, str]]:
        return self.repo.completed_turns(conversation_id, limit_turns)

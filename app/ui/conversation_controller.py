"""Liga a lista de conversas ao histórico (SQLite) e ao pipeline. Trocar de conversa:
  1) grava a conversa ativa e reconstrói o contexto da IA SÓ com as mensagens dela (nunca chama a IA);
  2) limpa a tela e carrega as mensagens da conversa escolhida, uma vez, na ordem;
  3) respostas ainda pendentes de outra conversa continuam sendo gravadas lá e não aparecem aqui."""
from __future__ import annotations

import logging

from PySide6.QtCore import QObject

from app.database.database import DatabaseError
from app.ui.history_restore import restore_history

log = logging.getLogger("jarvis.conversations")


class ConversationController(QObject):
    def __init__(self, window, panel, history, assistant, bridge=None, restore_messages: int = 100) -> None:
        super().__init__(window)
        self._window, self._panel, self._history, self._assistant, self._bridge = window, panel, history, assistant, bridge
        self._limit, self._busy = restore_messages, False
        window.current_conversation_id = history.active_id
        panel.new_requested.connect(self.new_conversation)
        panel.selected.connect(self.select)
        panel.rename_requested.connect(self.rename)
        panel.delete_requested.connect(self.delete)
        self.refresh()

    def refresh(self) -> None:
        try:
            self._panel.set_conversations(self._history.list_conversations(), self._history.active_id)
        except DatabaseError as exc:
            log.warning("[CONV] não consegui listar as conversas: %s", exc)
            self._window.set_status_message("Não consegui carregar a lista de conversas.")

    def _show(self, conversation_id: str) -> None:
        """Ativa a conversa no pipeline e a mostra na tela (uma única vez, sem duplicar)."""
        self._assistant.activate_conversation(conversation_id)  # grava como ativa, zera e reconstrói o contexto DELA
        self._window.current_conversation_id = conversation_id
        self._window.hide_confirmation()
        self._window.reset_chat()
        if self._bridge is not None:
            self._bridge.reset_for_conversation_switch()
        restore_history(self._window, self._history.load_messages(conversation_id, self._limit))
        self.refresh()

    def _guarded(self, fn, *args) -> None:
        if self._busy:  # clique duplo / troca rápida: um movimento por vez
            return
        self._busy = True
        try:
            fn(*args)
        except DatabaseError as exc:
            log.warning("[CONV] operação falhou: %s", exc)
            self._window.set_status_message(str(exc) or "Não consegui concluir a operação na conversa.")
            self.refresh()
        finally:
            self._busy = False

    def new_conversation(self) -> None:
        self._guarded(self._new)

    def _new(self) -> None:
        cid, created = self._history.new_conversation()  # se a ativa ainda está vazia, ela é a "nova"
        if created or self._window.current_conversation_id != cid:
            self._show(cid)
        else:
            self.refresh()
        self._window.set_status_message("Nova conversa" if created else "Você já está numa conversa vazia")

    def select(self, conversation_id: str) -> None:
        if conversation_id == self._history.active_id and conversation_id == self._window.current_conversation_id:
            return
        self._guarded(self._show, conversation_id)

    def rename(self, conversation_id: str, title: str) -> None:
        ok, message = self._history.rename_conversation(conversation_id, title)
        self._window.set_status_message("Conversa renomeada" if ok else message)
        self.refresh()

    def delete(self, conversation_id: str) -> None:
        self._guarded(self._delete, conversation_id)

    def _delete(self, conversation_id: str) -> None:
        was_active = conversation_id == self._history.active_id
        new_active = self._history.delete_conversation(conversation_id)
        self._window.set_status_message("Conversa excluída")
        if was_active:
            self._show(new_active)
        else:
            self.refresh()

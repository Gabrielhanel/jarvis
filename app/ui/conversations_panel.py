"""Lista de conversas (barra lateral). Só exibe e emite sinais; quem lê/grava o banco é o ConversationController."""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QInputDialog, QLabel, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout

ITEM_ID = Qt.ItemDataRole.UserRole


def when_label(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%d/%m %H:%M")
    except ValueError:
        return ""


class ConversationsPanel(QFrame):
    new_requested = Signal()
    selected = Signal(str)
    rename_requested = Signal(str, str)  # (id, novo título)
    delete_requested = Signal(str)  # só depois da confirmação do usuário

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("conversationsPanel")
        self.setMinimumWidth(200)
        self.setMaximumWidth(270)
        lay = QVBoxLayout(self)
        title = QLabel("Conversas")
        title.setObjectName("panelTitle")
        self.new_button = QPushButton("+ Nova conversa")
        self.new_button.setObjectName("newConversation")
        self.new_button.setAccessibleName("Iniciar uma nova conversa")
        self.list = QListWidget()
        self.list.setAccessibleName("Lista de conversas")
        self.list.setWordWrap(True)
        row = QHBoxLayout()
        self.rename_button, self.delete_button = QPushButton("Renomear"), QPushButton("Excluir")
        self.rename_button.setAccessibleName("Renomear a conversa selecionada")
        self.delete_button.setAccessibleName("Excluir a conversa selecionada")
        row.addWidget(self.rename_button)
        row.addWidget(self.delete_button)
        for w in (title, self.new_button, self.list):
            lay.addWidget(w)
        lay.addLayout(row)
        self._active: str | None = None
        self.new_button.clicked.connect(self.new_requested)
        self.list.itemClicked.connect(self._on_clicked)  # clique do USUÁRIO (carregar a lista por código não dispara)
        self.rename_button.clicked.connect(self._rename)
        self.delete_button.clicked.connect(self._delete)
        self._sync_buttons()

    def set_conversations(self, items, active_id: str | None) -> None:
        self._active = active_id
        self.list.blockSignals(True)
        self.list.clear()
        for c in items:
            it = QListWidgetItem(("● " if c.id == active_id else "") + c.title + f"\n{when_label(c.updated_at)} · {c.message_count} msg")
            it.setData(ITEM_ID, c.id)
            self.list.addItem(it)
            if c.id == active_id:
                self.list.setCurrentItem(it)
        self.list.blockSignals(False)
        self._sync_buttons()

    def current_id(self) -> str | None:
        it = self.list.currentItem()
        return it.data(ITEM_ID) if it else None

    def _current_title(self) -> str:
        it = self.list.currentItem()
        return it.text().lstrip("● ").split("\n")[0] if it else ""

    def _sync_buttons(self) -> None:
        has = self.current_id() is not None
        self.rename_button.setEnabled(has)
        self.delete_button.setEnabled(has)

    def _on_clicked(self, item: QListWidgetItem) -> None:
        self._sync_buttons()
        self.selected.emit(item.data(ITEM_ID))

    def _rename(self) -> None:
        cid = self.current_id()
        if cid is None:
            return
        text, ok = QInputDialog.getText(self, "Renomear conversa", "Novo título:", text=self._current_title())
        if ok:
            self.rename_requested.emit(cid, text)

    def _delete(self) -> None:
        cid = self.current_id()
        if cid is None:
            return
        answer = QMessageBox.question(self, "Excluir conversa", f"Excluir a conversa “{self._current_title()}”?\nAs mensagens dela serão apagadas. "
                                      "Memórias permanentes, métricas e rotinas não são afetadas.", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                      QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            self.delete_requested.emit(cid)

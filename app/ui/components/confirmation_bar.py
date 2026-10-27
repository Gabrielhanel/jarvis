"""Barra de confirmação de ações sensíveis. Só emite sinais: não conhece o executor, a IA nem o banco.

Mostra a ação EXATA e o destino, um prazo regressivo e os botões Confirmar/Cancelar. Cada confirmação tem um ID e a barra
só aceita UM clique por ID (o ID é descartado antes de o sinal sair), então clique duplo não confirma duas vezes."""
from __future__ import annotations

import math

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QWidget

from app.ui.styles.theme import SPACING


class ConfirmationBar(QFrame):
    confirmed = Signal(str)  # (id da confirmação)
    cancelled = Signal(str)
    expired = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("confirmBar")
        self.setVisible(False)
        row = QHBoxLayout(self)
        row.setContentsMargins(SPACING["md"], SPACING["sm"], SPACING["md"], SPACING["sm"])
        row.setSpacing(SPACING["sm"])
        self.text = QLabel()
        self.text.setObjectName("confirmText")
        self.text.setWordWrap(True)
        self.timer_label = QLabel()
        self.timer_label.setObjectName("confirmTimer")
        self.confirm_button = QPushButton("Confirmar")
        self.confirm_button.setObjectName("confirm")
        self.confirm_button.setAccessibleName("Confirmar ação")
        self.cancel_button = QPushButton("Cancelar")
        self.cancel_button.setObjectName("cancel")
        self.cancel_button.setAccessibleName("Cancelar ação")
        row.addWidget(self.text, 1)
        row.addWidget(self.timer_label)
        row.addWidget(self.confirm_button)
        row.addWidget(self.cancel_button)
        self._id: str | None = None
        self._remaining = 0
        self._ticker = QTimer(self)
        self._ticker.setInterval(1000)
        self._ticker.timeout.connect(self._tick)
        self.confirm_button.clicked.connect(self._on_confirm)
        self.cancel_button.clicked.connect(self._on_cancel)

    @property
    def pending_id(self) -> str | None:
        return self._id

    def show_request(self, pending_id: str, summary: str, ttl_s: float) -> None:
        """Substitui qualquer pedido anterior: a barra nunca mostra uma confirmação antiga."""
        self._id, self._remaining = pending_id, max(1, math.ceil(ttl_s))
        self.text.setText(f"Confirmar: {summary}")
        self.timer_label.setText(f"{self._remaining}s")
        self.confirm_button.setEnabled(True)
        self.cancel_button.setEnabled(True)
        self.setVisible(True)
        self._ticker.start()

    def clear(self, pending_id: str | None = None) -> None:
        """Esconde a barra. Com `pending_id`, só se for a confirmação mostrada agora (um aviso atrasado não apaga a nova)."""
        if pending_id is not None and pending_id != self._id:
            return
        self._ticker.stop()
        self._id = None
        self.setVisible(False)

    def _take(self) -> str | None:
        pid, self._id = self._id, None  # uso único
        self._ticker.stop()
        self.setVisible(False)
        return pid

    def _on_confirm(self) -> None:
        if (pid := self._take()) is not None:
            self.confirmed.emit(pid)

    def _on_cancel(self) -> None:
        if (pid := self._take()) is not None:
            self.cancelled.emit(pid)

    def _tick(self) -> None:
        self._remaining -= 1
        if self._remaining <= 0:
            if (pid := self._take()) is not None:
                self.expired.emit(pid)
            return
        self.timer_label.setText(f"{self._remaining}s")

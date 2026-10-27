"""Barra de status inferior: estado atual, mensagem de detalhe e informação lateral."""
from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

from app.ui.states import JarvisState
from app.ui.styles.theme import SPACING, STATE_COLORS


class StatusBar(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(SPACING["md"])
        self._state = QLabel()
        self._state.setObjectName("statusState")
        self._message = QLabel()
        self._message.setObjectName("statusMessage")
        self._info = QLabel()
        self._info.setObjectName("statusInfo")
        row.addWidget(self._state)
        row.addWidget(self._message, 1)
        row.addWidget(self._info)
        self.set_state(JarvisState.IDLE)

    def set_state(self, state: JarvisState) -> None:
        self._state.setText(f"● {state.label.upper()}")
        self._state.setStyleSheet(f"color: {STATE_COLORS[state]};")

    def set_message(self, text: str) -> None:
        self._message.setText(text)

    def set_info(self, text: str) -> None:
        self._info.setText(text)

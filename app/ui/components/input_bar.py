"""Barra de entrada: texto, enviar e microfone (visual). Só emite sinais."""
from __future__ import annotations

import uuid

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QPushButton, QWidget

from app.ui.styles.theme import SPACING


class InputBar(QWidget):
    message_submitted = Signal(str, str)  # (id da submissão, texto): o ID nasce aqui, na origem do evento
    microphone_clicked = Signal()
    retry_clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(SPACING["sm"])
        self.input = QLineEdit()
        self.input.setPlaceholderText("Digite uma mensagem e tecle Enter")
        self.input.setAccessibleName("Mensagem")
        self.send_button = QPushButton("Enviar")
        self.send_button.setObjectName("send")
        self.send_button.setEnabled(False)
        self.mic_button = QPushButton("Microfone")
        self.mic_button.setObjectName("mic")
        self.mic_button.setToolTip("Escutar um comando agora, sem dizer “Ei Jarvis” (modo de teste)")
        self.retry_button = QPushButton("Tentar novamente")
        self.retry_button.setObjectName("retry")
        self.retry_button.setVisible(False)
        row.addWidget(self.input, 1)
        row.addWidget(self.send_button)
        row.addWidget(self.mic_button)
        row.addWidget(self.retry_button)

        self.input.textChanged.connect(lambda text: self.send_button.setEnabled(bool(text.strip())))
        self.input.returnPressed.connect(self._submit)
        self.send_button.clicked.connect(self._submit)
        self.mic_button.clicked.connect(self.microphone_clicked)
        self.retry_button.clicked.connect(self.retry_clicked)

    def _submit(self) -> None:
        text = self.input.text().strip()
        if text:
            self.input.clear()
            self.message_submitted.emit(uuid.uuid4().hex, text)

    def show_retry(self, visible: bool) -> None:
        self.retry_button.setVisible(visible)

    def focus_input(self) -> None:
        self.input.setFocus()

"""Cabeçalho: nome, indicador de estado e botão de configurações (visual)."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from app.ui.states import JarvisState
from app.ui.styles.theme import SPACING, STATE_COLORS


class Header(QWidget):
    settings_clicked = Signal()
    voice_test_clicked = Signal()
    stop_speech_clicked = Signal()
    panels_clicked = Signal()
    conversations_clicked = Signal()
    morning_clicked = Signal()

    def __init__(self, title: str = "JARVIS", subtitle: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(SPACING["md"])
        names = QVBoxLayout()
        names.setSpacing(0)
        self._title = QLabel(title)
        self._title.setObjectName("title")
        font = QFont(self._title.font())
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 8)
        self._title.setFont(font)
        self._subtitle = QLabel(subtitle)
        self._subtitle.setObjectName("subtitle")
        names.addWidget(self._title)
        names.addWidget(self._subtitle)
        self._badge = QLabel()
        self._badge.setObjectName("badge")
        self.settings_button = QPushButton("⚙")
        self.settings_button.setObjectName("settings")
        self.settings_button.setToolTip("Configurações (em breve)")
        self.settings_button.setAccessibleName("Configurações")
        self._speaking = False
        self.voice_button = QPushButton("Testar voz")
        self.voice_button.setObjectName("voiceTest")
        self.voice_button.setToolTip("Fala uma frase de demonstração (teste local, sem IA). Esc interrompe a fala.")
        self.conversations_button = QPushButton("☰ Conversas")
        self.conversations_button.setObjectName("voiceTest")
        self.conversations_button.setToolTip("Mostrar ou esconder a lista de conversas")
        self.conversations_button.setAccessibleName("Mostrar ou esconder a lista de conversas")
        self.panels_button = QPushButton("Painéis")
        self.panels_button.setObjectName("voiceTest")  # mesmo estilo discreto do botão de voz
        self.panels_button.setToolTip("Agenda, e-mails, notícias, tempo e configurações das integrações")
        self.panels_button.setAccessibleName("Abrir painéis de agenda, e-mails, notícias e tempo")
        self.morning_button = QPushButton("☀ Resumo")
        self.morning_button.setObjectName("voiceTest")
        self.morning_button.setToolTip("Resumo da manhã: agenda, e-mails, notícias, clima e foco")
        self.morning_button.setAccessibleName("Resumo da manhã")
        row.addLayout(names)
        row.addStretch(1)
        row.addWidget(self._badge, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.conversations_button, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.morning_button, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.panels_button, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.voice_button, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.settings_button, 0, Qt.AlignmentFlag.AlignVCenter)
        self.settings_button.clicked.connect(self.settings_clicked)
        self.panels_button.clicked.connect(self.panels_clicked)
        self.conversations_button.clicked.connect(self.conversations_clicked)
        self.morning_button.clicked.connect(self.morning_clicked)
        self.voice_button.clicked.connect(lambda: (self.stop_speech_clicked if self._speaking else self.voice_test_clicked).emit())
        self.set_state(JarvisState.IDLE)

    def set_subtitle(self, text: str) -> None:
        self._subtitle.setText(text)

    def set_state(self, state: JarvisState) -> None:
        self._speaking = state is JarvisState.SPEAKING
        self.voice_button.setText("Parar fala" if self._speaking else "Testar voz")
        self._badge.setText(f'<span style="color:{STATE_COLORS[state]}">●</span>&nbsp; {state.label}')

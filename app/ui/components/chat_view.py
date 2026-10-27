"""Área de conversa: balões de mensagem numa rolagem. Só exibe; não gera respostas."""
from __future__ import annotations

from enum import Enum

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel, QScrollArea, QVBoxLayout, QWidget

from app.ui.styles.theme import SPACING


class MessageRole(Enum):
    USER = "Você"
    JARVIS = "Jarvis"
    SYSTEM = "Sistema"


_ALIGN = {MessageRole.USER: Qt.AlignmentFlag.AlignRight, MessageRole.JARVIS: Qt.AlignmentFlag.AlignLeft,
          MessageRole.SYSTEM: Qt.AlignmentFlag.AlignHCenter}


class MessageBubble(QFrame):
    PAD = 12

    def __init__(self, role: MessageRole, text: str) -> None:
        super().__init__()
        self._text = text
        self.setObjectName("bubble")
        self.setProperty("role", role.name.lower())  # usado pelo QSS
        layout = QVBoxLayout(self)
        layout.setContentsMargins(self.PAD, 8, self.PAD, 8)
        layout.setSpacing(2)
        name = self._name = QLabel(role.value)
        name.setObjectName("bubbleName")
        body = self._body = QLabel(text)
        body.setWordWrap(True)
        body.setTextFormat(Qt.TextFormat.PlainText)  # nunca interpreta HTML
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(name)
        layout.addWidget(body)

    def fit(self, max_width: int) -> None:
        """Define largura e altura explícitas: o Qt erra a altura de QLabel com quebra de linha
        dentro de layouts com alinhamento, e o texto sairia cortado."""
        for widget in (self, self._name, self._body):
            widget.ensurePolished()
        longest = max((self._body.fontMetrics().horizontalAdvance(line) for line in self._text.split("\n")), default=0)
        name_w = self._name.fontMetrics().horizontalAdvance(self._name.text())
        width = max(80, min(max_width, max(longest, name_w) + 2 * self.PAD + 8))
        self.setFixedWidth(width)
        self.setFixedHeight(self.layout().totalHeightForWidth(width) + 4)


class ChatView(QScrollArea):
    MAX_WIDTH_RATIO = .8

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("chat")
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMinimumHeight(120)
        inner = QWidget()
        inner.setObjectName("chatInner")
        self._layout = QVBoxLayout(inner)
        self._layout.setContentsMargins(SPACING["md"], SPACING["md"], SPACING["md"], SPACING["md"])
        self._layout.setSpacing(SPACING["sm"])
        self._hint = QLabel("Nenhuma mensagem ainda. Digite abaixo para começar.")
        self._hint.setObjectName("emptyHint")
        self._hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._layout.addWidget(self._hint)
        self._layout.addStretch(1)
        self.setWidget(inner)
        self._bubbles: list[MessageBubble] = []
        self._stick = True  # só acompanha novas mensagens se o usuário estiver no fim
        bar = self.verticalScrollBar()
        bar.rangeChanged.connect(lambda _lo, hi: bar.setValue(hi) if self._stick else None)
        bar.valueChanged.connect(lambda v: setattr(self, "_stick", v >= bar.maximum() - 4))

    def add_message(self, role: MessageRole, text: str) -> None:
        self._hint.hide()
        bubble = MessageBubble(role, text)
        self._bubbles.append(bubble)
        self._layout.insertWidget(self._layout.count() - 1, bubble, 0, _ALIGN[role])
        bubble.fit(self._max_bubble_width())

    def clear(self) -> None:
        for bubble in self._bubbles:
            bubble.deleteLater()
        self._bubbles.clear()
        self._hint.show()

    def _max_bubble_width(self) -> int:
        return max(160, int(self.viewport().width() * self.MAX_WIDTH_RATIO))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        for bubble in self._bubbles:
            bubble.fit(self._max_bubble_width())

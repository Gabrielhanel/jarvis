"""Janela principal: só compõe os componentes e repassa sinais. Sem regra de negócio."""
from __future__ import annotations

import uuid

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QBoxLayout, QMainWindow, QVBoxLayout, QWidget

from app import __version__
from app.core.config import Config
from app.core.modes import profile as _mode_profile
from app.ui.conversations_panel import ConversationsPanel
from app.ui.components import ChatView, ConfirmationBar, Header, InputBar, JarvisOrb, MessageRole, StatusBar
from app.ui.states import JarvisState
from app.ui.styles.theme import SPACING, build_stylesheet


class MainWindow(QMainWindow):
    message_submitted = Signal(str, str, str)  # (id, texto, origem: "texto" | "voz")
    retry_requested = Signal()
    action_confirmed = Signal(str)  # (id da confirmação)
    action_cancelled = Signal(str)
    action_expired = Signal(str)
    microphone_clicked = Signal()
    settings_clicked = Signal()
    voice_test_clicked = Signal()
    panels_requested = Signal()
    morning_requested = Signal()
    stop_speech_clicked = Signal()
    state_changed = Signal(object)  # JarvisState

    WIDE_BREAKPOINT = 1000  # a partir daqui, orb e conversa ficam lado a lado

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.setWindowTitle("Jarvis")
        self.resize(980, 740)
        self.setMinimumSize(560, 520)
        self.setStyleSheet(build_stylesheet(config.get("ui.accent")))
        self._state = JarvisState.IDLE
        self.ai_busy = False  # a IA está processando: a ponte de voz não sobrescreve o orb
        self._seen_ids: dict[str, None] = {}
        self._wide = False

        self.current_conversation_id: str | None = None  # a conversa que está NA TELA (a ponte só mostra respostas dela)
        self.header = Header("JARVIS", self._subtitle(config.get("mode"), config.ai_config().model))  # modelo EFETIVO (o enviado à API)
        self.sidebar = ConversationsPanel()
        self.orb = JarvisOrb()
        self.chat = ChatView()
        self.input_bar = InputBar()
        self.confirm_bar = ConfirmationBar()
        self.status_bar = StatusBar()
        self.status_bar.set_info(f"v{__version__}")

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(SPACING["lg"] + 6, SPACING["lg"], SPACING["lg"] + 6, SPACING["md"])
        outer.setSpacing(SPACING["md"])
        panel = QWidget()
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.setSpacing(SPACING["sm"])
        panel_layout.addWidget(self.chat, 1)
        panel_layout.addWidget(self.confirm_bar)
        panel_layout.addWidget(self.input_bar)
        self._body = QBoxLayout(QBoxLayout.Direction.TopToBottom)
        self._body.setSpacing(SPACING["lg"])
        self._body.addWidget(self.orb)
        self._body.addWidget(panel)
        outer.addWidget(self.header)
        content = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        content.setSpacing(SPACING["lg"])
        content.addWidget(self.sidebar)
        content.addLayout(self._body, 1)
        outer.addLayout(content, 1)
        outer.addWidget(self.status_bar)
        self._apply_layout()

        self.input_bar.message_submitted.connect(lambda sid, text: self._on_message_submitted(sid, text, "texto"))
        self.input_bar.retry_clicked.connect(self.retry_requested)
        self.confirm_bar.confirmed.connect(self.action_confirmed)
        self.confirm_bar.cancelled.connect(self.action_cancelled)
        self.confirm_bar.expired.connect(self.action_expired)
        self.input_bar.microphone_clicked.connect(self.microphone_clicked)
        self.header.settings_clicked.connect(self.settings_clicked)
        self.header.conversations_clicked.connect(self.toggle_sidebar)
        self.header.voice_test_clicked.connect(self.voice_test_clicked)
        self.header.panels_clicked.connect(self.panels_requested)
        self.header.morning_clicked.connect(self.morning_requested)
        self.header.stop_speech_clicked.connect(self.stop_speech_clicked)
        self.input_bar.focus_input()

    # --- API pública: é por aqui que os serviços futuros falam com a UI ---
    @property
    def state(self) -> JarvisState:
        return self._state

    def set_state(self, state: JarvisState | str) -> None:
        state = JarvisState.coerce(state)
        if state is self._state:
            return
        self._state = state
        self.orb.set_state(state)
        self.header.set_state(state)
        self.status_bar.set_state(state)
        self.state_changed.emit(state)

    def add_message(self, role: MessageRole, text: str) -> None:
        self.chat.add_message(role, text)

    def submit_text(self, text: str, submission_id: str | None = None, source: str = "voz") -> None:
        """Entrada de texto vinda de fora da caixa de digitação (ex.: voz): mesmo caminho do Enter."""
        self._on_message_submitted(submission_id or uuid.uuid4().hex, text, source)

    def show_retry(self, visible: bool) -> None:
        self.input_bar.show_retry(visible)

    @staticmethod
    def _subtitle(mode, model: str) -> str:
        return f"modo {_mode_profile(mode).label.lower()} · modelo {model}"

    def set_header_info(self, mode, model: str) -> None:
        self.header.set_subtitle(self._subtitle(mode, model))

    def toggle_sidebar(self) -> None:
        self.set_sidebar_visible(self.sidebar.isHidden())

    def set_sidebar_visible(self, visible: bool) -> None:
        self.sidebar.setVisible(visible)

    def reset_chat(self) -> None:
        """Esvazia SÓ o que está na tela (as conversas continuam no banco) para carregar outra conversa sem duplicar mensagens."""
        self.chat.clear()
        self._history_restored = False

    def show_confirmation(self, pending_id: str, summary: str, ttl_s: float) -> None:
        self.confirm_bar.show_request(pending_id, summary, ttl_s)

    def hide_confirmation(self, pending_id: str | None = None) -> None:
        self.confirm_bar.clear(pending_id)

    def set_status_message(self, text: str) -> None:
        self.status_bar.set_message(text)

    # --- interno ---
    def _on_message_submitted(self, submission_id: str, text: str, source: str) -> None:
        text = text.strip()
        if not text or submission_id in self._seen_ids:  # mesmo evento entregue duas vezes: ignorado
            return
        self._seen_ids[submission_id] = None
        if len(self._seen_ids) > 256:
            self._seen_ids.pop(next(iter(self._seen_ids)))
        self.add_message(MessageRole.USER, text)  # a mensagem do usuário entra na conversa uma única vez
        self.message_submitted.emit(submission_id, text, source)

    def _apply_layout(self) -> None:
        self._body.setDirection(QBoxLayout.Direction.LeftToRight if self._wide else QBoxLayout.Direction.TopToBottom)
        self._body.setStretch(0, 5 if self._wide else 3)
        self._body.setStretch(1, 6 if self._wide else 4)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        wide = self.width() >= self.WIDE_BREAKPOINT
        if wide != self._wide:
            self._wide = wide
            self._apply_layout()

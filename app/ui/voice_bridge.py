"""Liga os eventos de voz à interface. É o único ponto que conhece os dois lados.

Os slots ficam num QObject da thread principal: assim o Qt entrega cada sinal da thread de
voz na fila de eventos da UI, e nenhum widget é tocado fora da thread principal.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Slot

from app.services.voice_service import VoiceService, VoiceState
from app.ui.components import MessageRole
from app.ui.main_window import MainWindow
from app.ui.states import JarvisState

# Esperando a wake word o orb fica em repouso; só "escuta ativa" usa LISTENING.
_ORB = {VoiceState.IDLE: JarvisState.IDLE, VoiceState.WAITING_WAKEWORD: JarvisState.IDLE,
        VoiceState.LISTENING_COMMAND: JarvisState.LISTENING, VoiceState.TRANSCRIBING: JarvisState.THINKING,
        VoiceState.ERROR: JarvisState.ERROR}


class VoiceBridge(QObject):
    def __init__(self, window: MainWindow, voice: VoiceService) -> None:
        super().__init__(window)
        self._window, self._voice, self._mic = window, voice, ""
        voice.state_changed.connect(self._on_state)
        voice.microphone_started.connect(self._on_mic_started)
        voice.microphone_unavailable.connect(self._on_problem)
        voice.audio_error.connect(self._on_problem)
        voice.stt_unavailable.connect(self._on_problem)
        voice.model_loading.connect(self._on_model_loading)
        voice.wake_word_detected.connect(self._on_wake)
        voice.command_timeout.connect(self._on_timeout)
        voice.transcription_error.connect(self._on_transcription_error)
        voice.text_recognized.connect(self._on_text)
        window.microphone_clicked.connect(voice.trigger_manual)  # botão Microfone = escuta manual (teste)

    @Slot(object)
    def _on_state(self, state: VoiceState) -> None:
        self._apply(state)

    def refresh(self) -> None:
        """Reaplica o estado atual da voz (usado quando o Jarvis termina de falar)."""
        self._apply(self._voice.current_state)

    def _apply(self, state: VoiceState) -> None:
        if self._window.state is not JarvisState.SPEAKING and not self._window.ai_busy:  # falando/pensando, o orb não muda
            self._window.set_state(_ORB[state])
        if state is VoiceState.WAITING_WAKEWORD:
            self._window.set_status_message("Aguardando “Ei Jarvis”" + (f" · {self._mic}" if self._mic else ""))
        elif state is VoiceState.LISTENING_COMMAND:
            self._window.set_status_message("Ouvindo o comando...")
        elif state is VoiceState.TRANSCRIBING:
            self._window.set_status_message("Transcrevendo...")

    @Slot(str)
    def _on_mic_started(self, name: str) -> None:
        self._mic = name

    @Slot(str)
    def _on_problem(self, message: str) -> None:
        self._window.set_status_message(message)
        self._window.add_message(MessageRole.SYSTEM, f"{message}. Você ainda pode digitar mensagens.")

    @Slot()
    def _on_model_loading(self) -> None:
        self._window.set_status_message("Carregando modelo de voz...")

    @Slot()
    def _on_wake(self) -> None:
        self._window.set_status_message("Ei Jarvis detectado")

    @Slot()
    def _on_timeout(self) -> None:
        self._window.set_status_message("Não ouvi nenhum comando.")

    @Slot(str)
    def _on_transcription_error(self, message: str) -> None:
        self._window.set_status_message(message)

    @Slot(str, str)
    def _on_text(self, text: str, submission_id: str) -> None:
        self._window.submit_text(text, submission_id, "voz")  # mesmo caminho do texto digitado

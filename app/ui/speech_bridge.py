"""Liga o TTS à interface e ao microfone. Só orquestra eventos; não sintetiza nem decide o que dizer.

- UI: o orb fica em SPEAKING durante a fala e volta ao estado da voz quando ela termina, é
  interrompida ou falha (Esc, botão "Parar fala" ou stop()).
- Microfone: a captura da Etapa 3 é suspensa no início da fala e retomada após o fim (+ tempo de
  eco). O stream do microfone continua aberto. A suspensão/retomada usam conexão DIRETA (rodam na
  thread do TTS, antes do áudio sair) e só colocam um comando numa fila: são seguras entre threads.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Qt, Slot
from PySide6.QtGui import QKeySequence, QShortcut

from app.services.tts_service import TTSService
from app.services.voice_service import VoiceService
from app.ui.components import MessageRole
from app.ui.main_window import MainWindow
from app.ui.states import JarvisState
from app.ui.voice_bridge import VoiceBridge


class SpeechBridge(QObject):
    def __init__(self, window: MainWindow, tts: TTSService, voice: VoiceService | None = None,
                 voice_bridge: VoiceBridge | None = None) -> None:
        super().__init__(window)
        self._window, self._tts, self._voice_bridge = window, tts, voice_bridge
        tts.synthesis_started.connect(self._on_synthesis)
        tts.speaking_started.connect(self._on_started)
        tts.speaking_finished.connect(self._on_ended)
        tts.speaking_interrupted.connect(self._on_ended)
        tts.tts_error.connect(self._on_error)
        tts.unavailable.connect(self._on_unavailable)
        window.voice_test_clicked.connect(self._on_test)
        window.stop_speech_clicked.connect(tts.stop)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), window, activated=tts.stop)
        if voice is not None:
            direct = Qt.ConnectionType.DirectConnection
            holdoff = tts.config.resume_holdoff_ms
            tts.speaking_started.connect(voice.suspend, direct)
            for signal in (tts.speaking_finished, tts.speaking_interrupted, tts.tts_error):
                signal.connect(lambda *_: voice.resume(holdoff), direct)

    @Slot()
    def _on_test(self) -> None:
        if not self._tts.config.enabled:
            self._window.set_status_message("A voz do Jarvis está desativada na configuração (tts.enabled).")
            return
        self._tts.speak(self._tts.config.test_phrase, interrupt=True)  # frase fixa: não há IA

    @Slot()
    def _on_synthesis(self) -> None:
        self._window.set_status_message("Preparando a voz...")

    @Slot()
    def _on_started(self) -> None:
        self._window.set_state(JarvisState.SPEAKING)
        self._window.set_status_message("Falando...")

    @Slot()
    def _on_ended(self) -> None:
        self._restore()

    @Slot(str)
    def _on_error(self, message: str) -> None:
        self._restore()
        self._window.set_status_message(message)
        self._window.add_message(MessageRole.SYSTEM, f"Não consegui falar: {message}")

    @Slot(str)
    def _on_unavailable(self, message: str) -> None:
        self._window.set_status_message(message)
        self._window.add_message(MessageRole.SYSTEM, f"{message}. Você ainda pode usar o Jarvis por texto e voz de entrada.")

    def _restore(self) -> None:
        if self._window.state is JarvisState.SPEAKING:
            self._window.set_state(JarvisState.IDLE)
        if self._voice_bridge is not None:
            self._voice_bridge.refresh()  # volta ao estado real da captura de voz

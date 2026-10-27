"""Liga o HotkeyService ao Qt: filtro de eventos nativos (Windows), atalho LOCAL de reserva (rotulado como tal) e a ação de acionar o Jarvis.
O acionamento usa o mecanismo que já existe (o mesmo do botão do microfone) e respeita o estado: não abre uma segunda escuta, não
sobrepõe a fala nem uma chamada à IA em andamento."""
from __future__ import annotations

import logging

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QApplication

from app.services.hotkey_service import HotkeyService, HotkeyState, parse_combo

log = logging.getLogger("jarvis.hotkey")


class _NativeFilter(QAbstractNativeEventFilter):
    def __init__(self, backend) -> None:
        super().__init__()
        self._backend = backend

    def nativeEventFilter(self, event_type, message):  # noqa: N802 - Qt
        if bytes(event_type) == b"windows_generic_MSG":
            try:
                if self._backend.handle_native_message(int(message)):
                    return True, 0
            except Exception:  # noqa: BLE001 - nunca derrubar o laço de eventos
                log.exception("[HOTKEY] falha no filtro nativo")
        return False, 0


class HotkeyBridge(QObject):
    def __init__(self, window, service: HotkeyService, voice=None, tts=None, ai_busy=lambda: False) -> None:
        super().__init__(window)
        self._window, self.service, self._voice, self._tts, self._ai_busy = window, service, voice, tts, ai_busy
        self._local: QShortcut | None = None
        self._filter = None
        service._on_trigger = self.trigger  # o serviço só sabe "foi acionado"; o que fazer é daqui
        if service.supported and hasattr(service.backend, "handle_native_message"):
            self._filter = _NativeFilter(service.backend)
            QApplication.instance().installNativeEventFilter(self._filter)

    def apply(self, enabled: bool, combo: str):
        status = self.service.apply(enabled, combo)
        self._sync_local(enabled, combo, status.state)
        return status

    def _sync_local(self, enabled: bool, combo: str, state: HotkeyState) -> None:
        """Sem suporte global: um atalho LOCAL (só com o Jarvis em foco), claramente diferente do global."""
        if self._local is not None:
            self._local.setEnabled(False)
            self._local.deleteLater()
            self._local = None
        parsed, _err = parse_combo(combo)
        if enabled and state is HotkeyState.UNSUPPORTED and parsed is not None:
            self._local = QShortcut(QKeySequence(parsed.text), self._window)
            self._local.setContext(Qt.ShortcutContext.ApplicationShortcut)
            self._local.setAutoRepeat(False)
            self._local.activated.connect(self.service._fire)  # mesma trava contra repique

    def status_text(self) -> str:
        st = self.service.status
        if st.state is HotkeyState.UNSUPPORTED:
            local = f" Atalho LOCAL {st.combo} ativo (só com o Jarvis em foco)." if self._local is not None else ""
            return f"Atalho global indisponível: {st.message}{local}"
        return f"{st.state.value.capitalize()}" + (f": {st.message}" if st.message else "")

    def trigger(self) -> None:
        """Traz o Jarvis para a frente e, se estiver livre, começa a ouvir (como o botão do microfone)."""
        w = self._window
        w.show()
        w.setWindowState(w.windowState() & ~Qt.WindowState.WindowMinimized)
        w.raise_()
        w.activateWindow()
        w.input_bar.input.setFocus() if hasattr(w.input_bar, "input") else None
        if self._voice is None or not getattr(getattr(self._voice, "config", None), "enabled", False) or self._ai_busy():
            return
        speaking = self._tts is not None and self._tts.is_speaking() if hasattr(self._tts, "is_speaking") else False
        if speaking:
            return  # não interrompe a fala
        try:
            if self._voice.current_state().name == "WAITING_WAKEWORD":  # só se está livre, esperando a palavra de ativação
                self._voice.trigger_manual()
        except Exception:  # noqa: BLE001
            log.exception("[HOTKEY] não consegui iniciar a escuta")

    def shutdown(self) -> None:
        self.service.shutdown()
        if self._filter is not None:
            QApplication.instance().removeNativeEventFilter(self._filter)
            self._filter = None
        if self._local is not None:
            self._local.setEnabled(False)

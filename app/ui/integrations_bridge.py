"""Liga a janela principal ao serviço de integrações: botões do cabeçalho, painel, resumo da manhã (texto + voz) e avisos."""
from __future__ import annotations

from PySide6.QtCore import QObject, Slot

from app.services.integration_service import IntegrationService
from app.ui.components import MessageRole
from app.ui.integrations_panel import IntegrationsDialog


class IntegrationsBridge(QObject):
    def __init__(self, window, service: IntegrationService, tts=None) -> None:
        super().__init__(window)
        self._window, self._service, self._tts = window, service, tts
        self.dialog = IntegrationsDialog(service, window)
        window.panels_requested.connect(self._show_panels)
        window.morning_requested.connect(self._on_morning_clicked)
        service.morning_ready.connect(self._on_morning_ready)
        service.notice.connect(self._on_notice)

    @Slot()
    def _show_panels(self) -> None:
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    @Slot()
    def _on_morning_clicked(self) -> None:
        self._window.set_status_message("Montando o resumo da manhã…")
        self._service.request_morning()

    @Slot(str, bool)
    def _on_morning_ready(self, text: str, speak: bool) -> None:
        """O resumo sempre aparece em texto; só é falado se a preferência permitir. Não bloqueia: já veio pronto da thread de trabalho."""
        self._window.add_message(MessageRole.JARVIS, text)
        self._window.set_status_message("Resumo da manhã pronto")
        if speak and self._tts is not None and self._tts.config.enabled:
            self._tts.speak(text)

    @Slot(str)
    def _on_notice(self, message: str) -> None:
        self._window.set_status_message(message)

"""Modo demonstração: SÓ elementos visuais. Não há respostas simuladas; a conversa é sempre a real."""
from __future__ import annotations

from PySide6.QtGui import QKeySequence, QShortcut

from app.ui.components import MessageRole
from app.ui.main_window import MainWindow
from app.ui.states import JarvisState


def install_demo(window: MainWindow, show_hint: bool = True) -> None:
    # Mensagem do SISTEMA só visual: nunca é gravada no histórico nem enviada à IA (o contexto vem do AssistantService, nunca da tela).
    if show_hint:
        window.add_message(MessageRole.SYSTEM, "Dica: Ctrl+1 a Ctrl+6 trocam o estado do orb (apenas visual).")
    for number, state in enumerate(JarvisState, start=1):
        QShortcut(QKeySequence(f"Ctrl+{number}"), window, activated=lambda s=state: window.set_state(s))

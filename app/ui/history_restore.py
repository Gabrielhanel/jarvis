"""Mostra na tela o histórico restaurado do banco. Só exibe: não conhece SQL, IA nem TTS (nada é falado nem reenviado)."""
from __future__ import annotations

from typing import Iterable

from app.ui.components import MessageRole

_NOTES = {  # nota SÓ visual (não vai para o banco nem para a IA) ao lado de uma mensagem que não recebeu resposta
    "failed": "Esta mensagem não recebeu resposta (houve uma falha). Envie de novo se quiser.",
    "interrupted": "Esta mensagem ficou sem resposta porque o aplicativo foi encerrado durante o processamento. Nada foi reenviado.",
    "pending": "Esta mensagem ficou sem resposta porque o aplicativo foi encerrado durante o processamento. Nada foi reenviado.",
    "processing": "Esta mensagem ficou sem resposta porque o aplicativo foi encerrado durante o processamento. Nada foi reenviado.",
    "unconfirmed": "O resultado desta mensagem não foi confirmado (a IA pode ou não ter processado). Nada foi reenviado.",
}


def restore_history(window, messages: Iterable) -> int:
    """Insere as mensagens (objetos com .role, .content e .state) na área de conversa. Só age uma vez por janela."""
    if getattr(window, "_history_restored", False):
        return 0
    window._history_restored = True
    shown = 0
    for m in messages:
        if m.role == "user":
            window.add_message(MessageRole.USER, m.content)
            if m.state in _NOTES:
                window.add_message(MessageRole.SYSTEM, _NOTES[m.state])
        else:
            window.add_message(MessageRole.JARVIS, m.content)
        shown += 1
    return shown

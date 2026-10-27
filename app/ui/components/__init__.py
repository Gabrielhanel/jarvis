"""Componentes independentes da interface. Nenhum conhece IA, voz ou banco de dados."""

from .chat_view import ChatView, MessageRole
from .confirmation_bar import ConfirmationBar
from .header import Header
from .input_bar import InputBar
from .orb import JarvisOrb
from .status_bar import StatusBar

__all__ = ["ChatView", "ConfirmationBar", "Header", "InputBar", "JarvisOrb", "MessageRole", "StatusBar"]

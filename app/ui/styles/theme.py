"""Cores, tipografia, raios e espaçamentos. Mude aqui para mudar a identidade visual."""
from __future__ import annotations

from string import Template

from app.ui.states import JarvisState as S

COLORS = {
    "bg": "#04070e", "panel": "#070d18", "panel_alt": "#0a1220",
    "border": "#1a2c44", "border_strong": "#1d3550",
    "text": "#d6e4f2", "text_muted": "#8fa7c0", "text_dim": "#5b6b82",
    "accent": "#38d0ff", "accent_alt": "#a78bfa",
    "bubble_user": "#0c2236", "bubble_user_border": "#1b4a6b",
    "bubble_jarvis": "#161233", "bubble_jarvis_border": "#3a2f73",
}
STATE_COLORS = {  # mesmas famílias de cor dos estados do jarvis.html
    S.IDLE: "#4a9bc2", S.LISTENING: "#38d0ff", S.THINKING: "#fbbf24",
    S.SPEAKING: "#a78bfa", S.ERROR: "#ff5547", S.OFFLINE: "#5b6b82",
}
FONT_FAMILY = '"Segoe UI", "Inter", "Helvetica Neue", sans-serif'
FONT_SIZES = {"title": 24, "body": 14, "small": 12}
RADIUS = {"sm": 6, "md": 10, "pill": 10}
SPACING = {"xs": 4, "sm": 8, "md": 12, "lg": 18}

_QSS = Template("""
QMainWindow, QWidget#root { background: $bg; }
QWidget { color: $text; font-family: $font; font-size: ${fs_body}px; }
QLabel { background: transparent; }
QLabel#title { font-size: ${fs_title}px; font-weight: 700; color: $accent; }
QLabel#subtitle, QLabel#statusMessage, QLabel#statusInfo, QLabel#emptyHint { color: $text_muted; font-size: ${fs_small}px; }
QLabel#statusState { font-size: ${fs_small}px; font-weight: 600; }
QLabel#badge { background: $panel_alt; border: 1px solid $border_strong; border-radius: ${r_pill}px;
    padding: 3px 12px; font-size: ${fs_small}px; }
QScrollArea#chat { background: $panel; border: 1px solid $border; border-radius: ${r_md}px; }
QWidget#chatInner { background: $panel; }
QFrame#bubble { background: $panel_alt; border: 1px solid $border; border-radius: ${r_md}px; }
QFrame#bubble[role="user"] { background: $bubble_user; border-color: $bubble_user_border; }
QFrame#bubble[role="jarvis"] { background: $bubble_jarvis; border-color: $bubble_jarvis_border; }
QLabel#bubbleName { font-size: ${fs_small}px; font-weight: 600; color: $text_muted; }
QFrame#bubble[role="user"] QLabel#bubbleName { color: $accent; }
QFrame#bubble[role="jarvis"] QLabel#bubbleName { color: $accent_alt; }
QLineEdit { background: $panel; border: 1px solid $border; border-radius: ${r_sm}px; padding: 9px 12px;
    selection-background-color: $accent; selection-color: $bg; }
QLineEdit:focus { border-color: $accent; }
QPushButton { background: $panel_alt; border: 1px solid $border_strong; border-radius: ${r_sm}px; padding: 9px 16px; }
QPushButton:hover, QPushButton:focus { border-color: $accent; color: $accent; }
QPushButton:disabled { color: $text_dim; border-color: $border; }
QPushButton#send { border-color: $accent; color: $accent; }
QPushButton#send:disabled { border-color: $border; color: $text_dim; }
QPushButton#settings { padding: 0; font-size: 18px; min-width: 36px; max-width: 36px; min-height: 36px; max-height: 36px; }
QPushButton#voiceTest { padding: 6px 12px; font-size: ${fs_small}px; }
QFrame#confirmBar { background: $bubble_jarvis; border: 1px solid #fbbf24; border-radius: ${r_md}px; }
QLabel#confirmText { color: $text; }
QLabel#confirmTimer { color: $text_muted; font-size: ${fs_small}px; }
QPushButton#confirm { border-color: #fbbf24; color: #fbbf24; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: $border_strong; border-radius: 4px; min-height: 24px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
""")


def build_stylesheet(accent: str | None = None) -> str:
    values = dict(COLORS, font=FONT_FAMILY)
    if accent:
        values["accent"] = accent
    values.update({f"fs_{k}": v for k, v in FONT_SIZES.items()})
    values.update({f"r_{k}": v for k, v in RADIUS.items()})
    return _QSS.substitute(values)

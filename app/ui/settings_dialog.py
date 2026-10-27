"""Configurações do Jarvis, geradas a partir do esquema validado (app/core/user_settings.py). Cada mudança do USUÁRIO é validada e
gravada na hora; repetir o mesmo valor não faz nada. Nenhum segredo é pedido ou mostrado aqui."""
from __future__ import annotations

from typing import Callable

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton,
                               QScrollArea, QSpinBox, QVBoxLayout, QWidget)

from app.core.modes import MODES
from app.core.user_settings import SETTINGS, Setting
from app.services.metrics_service import PERIODS


def choice_label(key: str, choice: str) -> str:
    if key == "mode":
        return MODES[choice].label
    return PERIODS.get(choice, choice) if key == "metrics.default_period" else choice


class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None, *, ai_configured: Callable[[], bool] = lambda: False, hotkey_status: Callable[[], str] = lambda: "",
                 integrations=None, openers: dict[str, Callable[[], None]] | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Configurações do Jarvis")
        self.resize(640, 700)
        self.settings, self._hotkey_status, self.integrations = settings, hotkey_status, integrations
        self.widgets: dict[str, QWidget] = {}
        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        col = QVBoxLayout(inner)
        scroll.setWidget(inner)
        outer.addWidget(scroll)
        self.feedback = QLabel("")
        self.feedback.setWordWrap(True)
        outer.addWidget(self.feedback)
        sections: dict[str, QFormLayout] = {}
        for key, spec in SETTINGS.items():
            if spec.section not in sections:
                box = QGroupBox(spec.section)
                sections[spec.section] = QFormLayout(box)
                col.addWidget(box)
            w = self._make(spec)
            self.widgets[key] = w
            label = spec.label + (" (vale ao reabrir)" if spec.restart else "")
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            h.addWidget(w, 1)
            sections[spec.section].addRow(label, row)
            if spec.help:
                note = QLabel(spec.help)
                note.setWordWrap(True)
                sections[spec.section].addRow("", note)
        self.mode_note = QLabel()
        self.mode_note.setWordWrap(True)
        sections["Assistente e IA"].addRow("", self.mode_note)
        self.key_label = QLabel("Chave da Anthropic: " + ("configurada (arquivo .env ou variável de ambiente; não é mostrada aqui)." if ai_configured() else
                                                         "NÃO configurada. Defina ANTHROPIC_API_KEY no .env; sem ela só os comandos locais funcionam."))
        self.key_label.setWordWrap(True)
        sections["Assistente e IA"].addRow("", self.key_label)
        self.hotkey_label = QLabel(hotkey_status())
        self.hotkey_label.setWordWrap(True)
        sections["Atalho global"].addRow("Situação", self.hotkey_label)
        # resumo da manhã (a preferência já existe nas integrações da Etapa 8: uma só fonte)
        box = QGroupBox("Inicialização e resumo")
        v = QVBoxLayout(box)
        self.morning_auto, self.morning_speak = QCheckBox("Fazer o Resumo da Manhã ao abrir o Jarvis (uma vez por dia)"), QCheckBox("Falar o resumo")
        v.addWidget(self.morning_auto)
        v.addWidget(self.morning_speak)
        self.morning_auto.clicked.connect(lambda _c=False: self._on_morning())
        self.morning_speak.clicked.connect(lambda _c=False: self._on_morning())
        col.addWidget(box)
        box = QGroupBox("Painéis")
        h = QHBoxLayout(box)
        for text, name in (("Rotinas…", "routines"), ("Métricas…", "metrics"), ("Agenda, e-mails, notícias e tempo…", "panels")):
            b = QPushButton(text)
            if openers and name in openers:
                b.clicked.connect(lambda _c=False, f=openers[name]: f())
            else:
                b.setEnabled(False)
            h.addWidget(b)
            setattr(self, f"{name}_button", b)
        col.addWidget(box)
        col.addStretch(1)
        self.reload()

    # --- construção ---
    def _make(self, spec: Setting) -> QWidget:
        k = spec.key
        if spec.kind == "choice":
            w = QComboBox()
            for c in spec.choices:
                w.addItem(choice_label(k, c), c)
            w.activated.connect(lambda _i=0, key=k: self._on_user(key))
        elif spec.kind == "bool":
            w = QCheckBox()
            w.clicked.connect(lambda _c=False, key=k: self._on_user(key))
        elif spec.kind == "int":
            w = QSpinBox()
            w.setRange(int(spec.low), int(spec.high))
            w.editingFinished.connect(lambda key=k: self._on_user(key))
        elif spec.kind == "float":
            w = QDoubleSpinBox()
            w.setRange(spec.low, spec.high)
            w.setDecimals(2)
            w.setSingleStep(0.05)
            w.editingFinished.connect(lambda key=k: self._on_user(key))
        else:
            w = QLineEdit()
            w.editingFinished.connect(lambda key=k: self._on_user(key))
        w.setAccessibleName(spec.label)
        return w

    # --- valores ---
    def _load_one(self, key: str) -> None:
        spec, w, value = SETTINGS[key], self.widgets[key], self.settings.value(key)
        if spec.kind == "choice":
            if w.findData(value) < 0:
                w.addItem(f"{value} (personalizado)", value)
            w.setCurrentIndex(w.findData(value))
        elif spec.kind == "bool":
            w.setChecked(bool(value))
        elif spec.kind in ("int", "float"):
            w.setValue(value)
        else:
            w.setText(str(value))
        if key == "mode":
            self.mode_note.setText(MODES[value].description)

    def reload(self) -> None:
        for key in SETTINGS:
            self._load_one(key)
        if self.integrations is not None:
            cfg = self.integrations.config
            self.morning_auto.setChecked(cfg.morning_auto)
            self.morning_speak.setChecked(cfg.morning_speak)
        else:
            self.morning_auto.setEnabled(False)
            self.morning_speak.setEnabled(False)
        self.hotkey_label.setText(self._hotkey_status())

    def _read(self, key: str):
        spec, w = SETTINGS[key], self.widgets[key]
        return (w.currentData() if spec.kind == "choice" else w.isChecked() if spec.kind == "bool" else w.value() if spec.kind in ("int", "float") else w.text())

    def _on_user(self, key: str) -> None:
        r = self.settings.set(key, self._read(key))
        self.feedback.setText(("✔ " if r.ok else "✖ ") + r.message)
        if not r.ok or key == "mode":
            self._load_one(key)  # recusou: volta ao valor válido; modo: atualiza a explicação
        self.hotkey_label.setText(self._hotkey_status())

    def _on_morning(self) -> None:
        if self.integrations is None:
            return
        cfg = self.integrations.config
        ok, msg = self.integrations.set_morning(self.morning_auto.isChecked(), self.morning_speak.isChecked(), cfg.morning_ai_polish)
        self.feedback.setText(("✔ " if ok else "✖ ") + msg)

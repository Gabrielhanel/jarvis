"""Painel de métricas locais: texto simples, só metadados. Uma falha ao carregar nunca atrapalha o resto do Jarvis."""
from __future__ import annotations

import logging

from PySide6.QtWidgets import QComboBox, QDialog, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout

from app.services.metrics_service import PERIODS, format_summary

log = logging.getLogger("jarvis.metrics")


class MetricsDialog(QDialog):
    def __init__(self, metrics, default_period: str = "7d", parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Métricas de uso (somente neste computador)")
        self.resize(560, 520)
        self.metrics = metrics
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.period = QComboBox()
        for key, label in PERIODS.items():
            self.period.addItem(label, key)
        self.period.setCurrentIndex(max(0, self.period.findData(default_period)))
        self.refresh_button, self.clear_button = QPushButton("Atualizar"), QPushButton("Limpar métricas…")
        top.addWidget(QLabel("Período:"))
        top.addWidget(self.period, 1)
        top.addWidget(self.refresh_button)
        top.addWidget(self.clear_button)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setAccessibleName("Resumo das métricas de uso")
        lay.addLayout(top)
        lay.addWidget(self.text)
        self.period.currentIndexChanged.connect(self.reload)
        self.refresh_button.clicked.connect(self.reload)
        self.clear_button.clicked.connect(self._clear)
        self.reload()

    def reload(self) -> None:
        try:
            self.text.setPlainText(format_summary(self.metrics.summary(self.period.currentData())))
        except Exception:  # noqa: BLE001 - métricas indisponíveis não podem afetar o aplicativo
            log.exception("[METRICS] falha ao carregar")
            self.text.setPlainText("Não consegui carregar as métricas agora. O restante do Jarvis continua funcionando.")

    def _clear(self) -> None:
        answer = QMessageBox.question(self, "Limpar métricas", "Apagar TODAS as métricas de uso?\nConversas, mensagens, memórias, rotinas, preferências e dados "
                                      "das integrações não são afetados. Isso não pode ser desfeito.", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                      QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            try:
                n = self.metrics.clear()
            except Exception:  # noqa: BLE001
                log.exception("[METRICS] falha ao limpar")
                self.text.setPlainText("Não consegui limpar as métricas agora.")
                return
            self.reload()
            self.text.appendPlainText(f"\n{n} registro(s) de uso apagado(s).")

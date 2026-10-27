"""Rotinas: lista, editor e resultado das últimas execuções. Executar uma rotina passa pelo MESMO caminho de uma mensagem do usuário
(histórico, confirmações, métricas), então não existe um segundo executor aqui."""
from __future__ import annotations

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit,
                               QPushButton, QVBoxLayout)

from app.core.routines import STEP_TYPES, Routine, RoutineStep, describe_step, validate_step
from app.services.routine_service import RoutineService


class RoutineEditor(QDialog):
    def __init__(self, service: RoutineService, routine: Routine | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Editar rotina" if routine else "Nova rotina")
        self.resize(520, 520)
        self.service, self.routine = service, routine
        self.steps: list[dict] = [s.to_dict() for s in routine.steps] if routine else []
        lay = QVBoxLayout(self)
        self.name = QLineEdit(routine.name if routine else "")
        self.name.setPlaceholderText("Nome da rotina (ex.: Manhã de trabalho)")
        self.trigger = QLineEdit(routine.trigger if routine else "")
        self.trigger.setPlaceholderText("Frase de voz opcional (ex.: modo trabalho) — ou diga “execute a rotina <nome>”")
        self.list = QListWidget()
        self.type = QComboBox()
        for key, spec in STEP_TYPES.items():
            self.type.addItem(spec.label, key)
        self.option = QComboBox()
        self.text = QLineEdit()
        self.essential = QCheckBox("Etapa essencial (se falhar, a rotina para)")
        self.essential.setChecked(True)
        add, rm, up, down = QPushButton("Adicionar etapa"), QPushButton("Remover"), QPushButton("▲"), QPushButton("▼")
        self.error = QLabel("")
        self.error.setWordWrap(True)
        save, cancel = QPushButton("Salvar"), QPushButton("Cancelar")
        row1, row2, row3 = QHBoxLayout(), QHBoxLayout(), QHBoxLayout()
        for w in (self.type, self.option, self.text):
            row1.addWidget(w)
        for w in (add, rm, up, down):
            row2.addWidget(w)
        for w in (save, cancel):
            row3.addWidget(w)
        for w in (QLabel("Nome"), self.name, QLabel("Frase de voz"), self.trigger, QLabel("Etapas (executadas em ordem)"), self.list, row1, self.essential, row2, self.error, row3):
            (lay.addLayout(w) if isinstance(w, QHBoxLayout) else lay.addWidget(w))
        self.type.activated.connect(lambda _i=0: self._type_changed())
        add.clicked.connect(self._add)
        rm.clicked.connect(self._remove)
        up.clicked.connect(lambda: self._move(-1))
        down.clicked.connect(lambda: self._move(1))
        save.clicked.connect(self._save)
        cancel.clicked.connect(self.reject)
        self._type_changed()
        self._render()

    def _type_changed(self) -> None:
        spec = STEP_TYPES[self.type.currentData()]
        self.option.clear()
        for value, label in zip(spec.options, spec.option_labels):
            self.option.addItem(label, value)
        self.option.setVisible(bool(spec.options))
        self.text.setVisible(spec.needs_value and not spec.options)
        self.text.setPlaceholderText("Nome do destino cadastrado" if self.type.currentData() != "spotify_search" else "O que pesquisar")
        self.essential.setEnabled(not spec.confirms)

    def _render(self) -> None:
        self.list.clear()
        for i, raw in enumerate(self.steps, 1):
            step, _ = validate_step(raw, i)
            self.list.addItem(f"{i}. " + (describe_step(step) + ("" if step.essential else "  (opcional)") if step else "etapa inválida"))

    def _add(self) -> None:
        spec = STEP_TYPES[self.type.currentData()]
        value = self.option.currentData() if spec.options else self.text.text() if spec.needs_value else ""
        raw = {"type": self.type.currentData(), "value": value, "essential": self.essential.isChecked()}
        step, err = validate_step(raw, len(self.steps) + 1)
        if err:
            self.error.setText("✖ " + err)
            return
        self.steps.append(step.to_dict())
        self.text.clear()
        self.error.setText("")
        self._render()

    def _remove(self) -> None:
        r = self.list.currentRow()
        if 0 <= r < len(self.steps):
            del self.steps[r]
            self._render()

    def _move(self, delta: int) -> None:
        r = self.list.currentRow()
        if 0 <= r < len(self.steps) and 0 <= r + delta < len(self.steps):
            self.steps[r], self.steps[r + delta] = self.steps[r + delta], self.steps[r]
            self._render()
            self.list.setCurrentRow(r + delta)

    def _save(self) -> None:
        routine, errors = self.service.save(self.name.text(), self.trigger.text(), self.steps, rid=self.routine.id if self.routine else None)
        if errors:
            self.error.setText("✖ " + " ".join(errors))
            return
        self.accept()


class RoutinesDialog(QDialog):
    run_requested = Signal(str)  # nome da rotina: a janela principal envia como uma mensagem do usuário

    def __init__(self, service: RoutineService, confirm_before_run=lambda: False, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Rotinas")
        self.resize(640, 520)
        self.service, self._confirm = service, confirm_before_run
        lay = QVBoxLayout(self)
        note = QLabel("Rotinas só combinam ações que o Jarvis já faz, com as mesmas permissões e confirmações. Não há scripts nem agendamento automático: "
                      "execute pelo botão ou por voz (“execute a rotina <nome>”).")
        note.setWordWrap(True)
        self.list, self.detail = QListWidget(), QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.new_b, self.edit_b, self.del_b, self.toggle_b, self.run_b = (QPushButton(t) for t in ("Nova", "Editar", "Excluir", "Ativar/Desativar", "Executar"))
        row = QHBoxLayout()
        for b in (self.new_b, self.edit_b, self.del_b, self.toggle_b, self.run_b):
            row.addWidget(b)
        for w in (note, self.list, row, self.detail):
            (lay.addLayout(w) if isinstance(w, QHBoxLayout) else lay.addWidget(w))
        self.list.currentRowChanged.connect(lambda _r: self._show_detail())
        self.new_b.clicked.connect(self._new)
        self.edit_b.clicked.connect(self._edit)
        self.del_b.clicked.connect(self._delete)
        self.toggle_b.clicked.connect(self._toggle)
        self.run_b.clicked.connect(self._run)
        self._items: list[Routine] = []
        self.reload()

    def reload(self) -> None:
        keep = self._selected().id if self._selected() else None
        self._items = self.service.list()
        self.list.clear()
        for r in self._items:
            self.list.addItem(QListWidgetItem(r.name + ("" if r.enabled else "  [desativada]") + ("" if r.valid else "  [dados inválidos]")))
        if self._items:
            self.list.setCurrentRow(next((i for i, r in enumerate(self._items) if r.id == keep), 0))
        self._show_detail()

    def _selected(self) -> Routine | None:
        r = self.list.currentRow()
        return self._items[r] if 0 <= r < len(self._items) else None

    def _show_detail(self) -> None:
        r = self._selected()
        if r is None:
            self.detail.setPlainText("Nenhuma rotina. Clique em “Nova”.")
            return
        lines = [f"Rotina: {r.name} ({'ativa' if r.enabled else 'desativada'})", f"Frase de voz: {r.trigger or '—'}", "", "Etapas:"] + [f"  {i}. {describe_step(s)}" + ("" if s.essential else " (opcional)") for i, s in enumerate(r.steps, 1)]
        runs = self.service.repo.last_runs(r.id, 5)
        lines += ["", "Últimas execuções:"] + ([f"  {x['started_at'][:16].replace('T', ' ')} UTC · {x['status']} · {x['source']} · " + " ".join(f"{e['i']}:{e['status']}" for e in x["results"]) for x in runs] or ["  (nenhuma ainda)"])
        self.detail.setPlainText("\n".join(lines))

    def _new(self) -> None:
        if RoutineEditor(self.service, None, self).exec():
            self.reload()

    def _edit(self) -> None:
        r = self._selected()
        if r and RoutineEditor(self.service, r, self).exec():
            self.reload()

    def _delete(self) -> None:
        r = self._selected()
        if r and QMessageBox.question(self, "Excluir rotina", f"Excluir a rotina “{r.name}”? Isso não pode ser desfeito.", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                      QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self.service.delete(r.id)
            self.reload()

    def _toggle(self) -> None:
        r = self._selected()
        if r:
            self.service.set_enabled(r.id, not r.enabled)
            self.reload()

    def _run(self) -> None:
        r = self._selected()
        if r is None or not r.enabled:
            return
        if self._confirm() and QMessageBox.question(self, "Executar rotina", f"Executar a rotina “{r.name}” agora?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        self.run_b.setEnabled(False)  # evita clique duplo; a execução em si também é protegida no serviço
        QTimer.singleShot(2000, lambda: (self.run_b.setEnabled(True), self.reload()))
        self.run_requested.emit(r.name)

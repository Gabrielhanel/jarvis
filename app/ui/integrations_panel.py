"""Painéis das integrações: Agenda, E-mails, Notícias, Tempo e Configurações. Só exibe e repassa ações ao IntegrationService;
não faz rede nem guarda segredos. Campos de credencial são limpos ao salvar e NUNCA mostram o valor de volta."""
from __future__ import annotations

from datetime import datetime, timedelta

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit,
                               QPushButton, QScrollArea, QTabWidget, QVBoxLayout, QWidget)

from app.integrations import answers
from app.integrations.common import ServiceState, ServiceStatus
from app.integrations.config import MAX_CALENDARS
from app.integrations.gmail import ACTION, INFO, NOISE
from app.integrations.manager import LABELS
from app.services.integration_service import IntegrationService

FILTERS = (("Todos", None), ("Ação", ACTION), ("Info", INFO), ("Ruído", NOISE))
STATE_ICON = {ServiceState.OK: "●", ServiceState.UPDATING: "◌", ServiceState.CACHED: "◐", ServiceState.ERROR: "✕", ServiceState.UNCONFIGURED: "○"}


def status_line(st: ServiceStatus, now: datetime | None = None) -> str:
    when = f" · última atualização {answers.when_text(st.last_ok, now or datetime.now().astimezone())}" if st.last_ok else ""
    detail = f" — {st.message}" if st.message and st.state is not ServiceState.OK else ""
    return f"{STATE_ICON[st.state]} {st.state.value}{detail}{when}"


class _Tab(QWidget):
    """Base: linha de estado + botão Atualizar + conteúdo."""

    def __init__(self, service: IntegrationService, name: str) -> None:
        super().__init__()
        self.service, self.name = service, name
        self.layout_ = QVBoxLayout(self)
        top = QHBoxLayout()
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setObjectName("integrationStatus")
        self.refresh_button = QPushButton("Atualizar")
        self.refresh_button.setAccessibleName(f"Atualizar {LABELS[name]}")
        self.refresh_button.clicked.connect(lambda: service.refresh(name))
        top.addWidget(self.status, 1)
        top.addWidget(self.refresh_button)
        self.layout_.addLayout(top)

    def update_status(self) -> None:
        st = self.service.manager.status(self.name)
        self.status.setText(status_line(st))
        self.refresh_button.setEnabled(st.state not in (ServiceState.UNCONFIGURED, ServiceState.UPDATING))

    def reload(self) -> None:  # redefinido pelas abas
        pass


class AgendaTab(_Tab):
    def __init__(self, service: IntegrationService) -> None:
        super().__init__(service, "calendar")
        self.list = QListWidget()
        self.list.setAccessibleName("Compromissos dos próximos dias")
        self.layout_.addWidget(self.list, 1)

    def reload(self) -> None:
        self.list.clear()
        cal = self.service.manager.calendar
        now = self.service.manager.now()
        if cal.status().state is ServiceState.UNCONFIGURED:
            self.list.addItem("Configure um calendário na aba Configurações.")
            return
        d0 = datetime.combine(now.date(), datetime.min.time(), tzinfo=now.tzinfo)
        events = cal.events(d0, d0 + timedelta(days=8))
        if not events:
            self.list.addItem("Nenhum compromisso nos próximos 7 dias." if cal.last_ok() else "Ainda não há dados da agenda.")
            return
        multi = len(cal.slots()) > 1
        for e in events:
            day = "Hoje" if e.start.date() == now.date() else "Amanhã" if e.start.date() == now.date() + timedelta(days=1) else f"{answers.DAYS[e.start.weekday()].capitalize()} {e.start:%d/%m}"
            hours = "dia inteiro" if e.all_day else f"{e.start:%H:%M}–{e.end:%H:%M}" if e.end > e.start else f"{e.start:%H:%M}"
            self.list.addItem(f"{day} · {hours} · {e.title}" + (f"  ({', '.join(e.calendars)})" if multi else ""))


class EmailTab(_Tab):
    def __init__(self, service: IntegrationService) -> None:
        super().__init__(service, "email")
        self._filter: str | None = None
        bar = QHBoxLayout()
        self.filter_buttons: dict[str | None, QPushButton] = {}
        for label, cat in FILTERS:
            b = QPushButton(label)
            b.setCheckable(True)
            b.setChecked(cat is None)
            b.clicked.connect(lambda _c=False, c=cat: self._set_filter(c))
            self.filter_buttons[cat] = b
            bar.addWidget(b)
        bar.addStretch(1)
        self.ai_button = QPushButton("Classificar com IA")
        self.ai_button.setToolTip("Uma chamada em lote só para as mensagens em dúvida (assunto, remetente e trecho curto).")
        self.ai_button.clicked.connect(service.classify_emails)
        bar.addWidget(self.ai_button)
        self.layout_.addLayout(bar)
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.layout_.addWidget(self.note)
        self.list = QListWidget()
        self.list.setAccessibleName("Caixa de entrada")
        self.list.currentItemChanged.connect(self._show_summary)
        self.layout_.addWidget(self.list, 2)
        self.detail = QPlainTextEdit()  # texto puro e somente leitura: nunca renderiza HTML de e-mail
        self.detail.setReadOnly(True)
        self.detail.setPlaceholderText("Selecione uma mensagem. “Consultar detalhes” lê o texto de novo no Gmail (somente leitura).")
        self.layout_.addWidget(self.detail, 2)
        self.detail_button = QPushButton("Consultar detalhes")
        self.detail_button.clicked.connect(self._ask_detail)
        self.layout_.addWidget(self.detail_button)
        service.email_detail_ready.connect(self._detail_ready)

    def _set_filter(self, cat: str | None) -> None:
        self._filter = cat
        for c, b in self.filter_buttons.items():
            b.setChecked(c == cat)
        self.reload()

    def reload(self) -> None:
        self.list.clear()
        mail = self.service.manager.email
        st = mail.status()
        if st.state is ServiceState.UNCONFIGURED:
            self.list.addItem("Configure a conta na aba Configurações.")
            self.note.setText("")
            return
        items = mail.items(self._filter)
        c = mail.counts()
        self.note.setText(f"Ação {c[ACTION]} · Info {c[INFO]} · Ruído {c[NOISE]}" + (" · classificação simplificada (regras locais)" if mail.simplified() and mail.items() else ""))
        if not items:
            self.list.addItem("Nenhuma mensagem nesta categoria." if mail.last_ok() else "Ainda não há dados da caixa de entrada.")
        for i in items:
            it = QListWidgetItem(f"{'● ' if i.unread else ''}[{i.category}] {i.subject} — {i.sender}")
            it.setData(Qt.ItemDataRole.UserRole, i.id)
            self.list.addItem(it)

    def _current_id(self) -> str | None:
        it = self.list.currentItem()
        return it.data(Qt.ItemDataRole.UserRole) if it else None

    def _show_summary(self, *_args) -> None:
        mid = self._current_id()
        item = next((i for i in self.service.manager.email.items() if i.id == mid), None) if mid else None
        if item is None:
            return
        self.detail.setPlainText(f"De: {item.sender} <{item.sender_addr}>\nAssunto: {item.subject}\nData: {item.date or 'não informada'}\n"
                                 f"Categoria: {item.category} ({'IA' if item.source == 'ia' else 'regras locais'}) — {item.reason}\n\n{item.snippet}")

    def _ask_detail(self) -> None:
        if (mid := self._current_id()):
            self.detail.appendPlainText("\n\nConsultando a mensagem…")
            self.service.fetch_email_detail(mid)

    def _detail_ready(self, email_id: str, text: str, ok: bool) -> None:
        if email_id == self._current_id():
            self._show_summary()
            self.detail.appendPlainText("\n— Texto da mensagem —\n" + text if ok else "\n" + text)


class NewsTab(_Tab):
    def __init__(self, service: IntegrationService) -> None:
        super().__init__(service, "news")
        self.list = QListWidget()
        self.list.setAccessibleName("Notícias")
        self.list.itemDoubleClicked.connect(lambda _i: self._open())
        self.layout_.addWidget(self.list, 1)
        self.open_button = QPushButton("Abrir no navegador")
        self.open_button.clicked.connect(self._open)
        self.layout_.addWidget(self.open_button)

    def reload(self) -> None:
        self.list.clear()
        news = self.service.manager.news
        groups = news.by_topic()
        if not any(groups.values()):
            self.list.addItem("Ainda não há notícias salvas." if news.last_ok() is None else "Sem notícias no momento.")
            return
        for topic, items in groups.items():
            head = QListWidgetItem(topic.upper())
            head.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list.addItem(head)
            for n in items:
                it = QListWidgetItem(f"  {n.title} — {n.source}")
                it.setData(Qt.ItemDataRole.UserRole, n.link)
                self.list.addItem(it)

    def _open(self) -> None:
        it = self.list.currentItem()
        if it and it.data(Qt.ItemDataRole.UserRole):
            self.service.open_news(it.data(Qt.ItemDataRole.UserRole))  # só por ação do usuário; o endereço é validado antes


class WeatherTab(_Tab):
    def __init__(self, service: IntegrationService) -> None:
        super().__init__(service, "weather")
        self.text = QLabel()
        self.text.setWordWrap(True)
        self.text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.layout_.addWidget(self.text)
        self.layout_.addStretch(1)

    def reload(self) -> None:
        w, now = self.service.manager.weather, self.service.manager.now()
        self.text.setText("\n\n".join([answers.weather_text(w, "now", False, now), answers.weather_text(w, "tomorrow", False, now)])
                          if w.status().state is not ServiceState.UNCONFIGURED else answers.WEATHER_UNCONFIGURED)


class SettingsTab(QWidget):
    def __init__(self, service: IntegrationService) -> None:
        super().__init__()
        self.service, self.m = service, service.manager
        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        form = QVBoxLayout(inner)
        scroll.setWidget(inner)
        outer.addWidget(scroll)
        self.feedback = QLabel()
        self.feedback.setWordWrap(True)
        self.feedback.setObjectName("integrationFeedback")
        outer.addWidget(self.feedback)
        self.vault = QLabel()
        self.vault.setWordWrap(True)
        form.addWidget(self.vault)

        # Calendários (até MAX_CALENDARS)
        self.cal_rows: list[tuple[str, QLineEdit, QLineEdit, QLabel]] = []
        box = QGroupBox("Google Calendar (endereço iCal secreto)")
        v = QVBoxLayout(box)
        help_ = QLabel("No Google Calendar: Configurações do calendário → Integrar agenda → “Endereço secreto no formato iCal”. "
                       "Esse endereço dá acesso aos seus eventos: é guardado no cofre do sistema e não é mostrado de novo.")
        help_.setWordWrap(True)
        v.addWidget(help_)
        for n in range(MAX_CALENDARS):
            sid = f"cal{n + 1}"
            row, name, url, state = QHBoxLayout(), QLineEdit(), QLineEdit(), QLabel()
            name.setPlaceholderText(f"Nome do calendário {n + 1} (ex.: Pessoal)")
            url.setPlaceholderText("Cole o endereço iCal secreto")
            url.setEchoMode(QLineEdit.EchoMode.Password)
            save, rm = QPushButton("Salvar"), QPushButton("Remover")
            save.clicked.connect(lambda _c=False, s=sid, a=name, b=url: self._save_calendar(s, a, b))
            rm.clicked.connect(lambda _c=False, s=sid: self._remove_calendar(s))
            for w in (name, url, save, rm):
                row.addWidget(w)
            v.addLayout(row)
            v.addWidget(state)
            self.cal_rows.append((sid, name, url, state))
        form.addWidget(box)

        # Gmail
        box = QGroupBox("Gmail (somente leitura)")
        v = QVBoxLayout(box)
        h2 = QLabel("Use uma SENHA DE APLICATIVO do Google (Conta Google → Segurança → Verificação em duas etapas → Senhas de app). "
                    "Não use a senha normal da conta. O Jarvis só lê: não envia, não apaga, não arquiva e não marca como lido.")
        h2.setWordWrap(True)
        v.addWidget(h2)
        self.email_address, self.email_password, self.email_state = QLineEdit(), QLineEdit(), QLabel()
        self.email_address.setPlaceholderText("seu.endereco@gmail.com")
        self.email_password.setPlaceholderText("Senha de aplicativo (16 letras)")
        self.email_password.setEchoMode(QLineEdit.EchoMode.Password)
        row = QHBoxLayout()
        save, rm = QPushButton("Salvar"), QPushButton("Remover")
        save.clicked.connect(self._save_email)
        rm.clicked.connect(self._remove_email)
        for w in (self.email_address, self.email_password, save, rm):
            row.addWidget(w)
        v.addLayout(row)
        v.addWidget(self.email_state)
        form.addWidget(box)

        # Tempo e notícias
        box = QGroupBox("Tempo (Open-Meteo) e notícias (Google News)")
        f = QFormLayout(box)
        self.city, self.country, self.topics = QLineEdit(), QLineEdit(), QLineEdit()
        self.city.setPlaceholderText("Cidade (ex.: Curitiba)")
        self.country.setPlaceholderText("País (opcional, 2 letras: BR)")
        self.country.setMaxLength(2)
        self.topics.setPlaceholderText("tecnologia, economia")
        sw, sn = QPushButton("Salvar cidade"), QPushButton("Salvar assuntos")
        sw.clicked.connect(self._save_weather)
        sn.clicked.connect(self._save_news)
        f.addRow("Cidade", self.city)
        f.addRow("País", self.country)
        f.addRow("", sw)
        f.addRow("Assuntos", self.topics)
        f.addRow("", sn)
        form.addWidget(box)

        # Resumo da manhã
        box = QGroupBox("Resumo da manhã")
        v = QVBoxLayout(box)
        self.auto, self.speak, self.polish = (QCheckBox("Fazer o resumo automaticamente ao abrir o Jarvis (uma vez por dia)"), QCheckBox("Falar o resumo"),
                                              QCheckBox("Reescrever com IA (1 chamada; envia o texto do resumo à Anthropic)"))
        for w in (self.auto, self.speak, self.polish):
            v.addWidget(w)
        row = QHBoxLayout()
        sm, now_ = QPushButton("Salvar preferências"), QPushButton("Gerar resumo agora")
        sm.clicked.connect(self._save_morning)
        now_.clicked.connect(service.request_morning)
        row.addWidget(sm)
        row.addWidget(now_)
        v.addLayout(row)
        form.addWidget(box)
        form.addStretch(1)
        service.notice.connect(self.feedback.setText)
        self.reload()

    def reload(self) -> None:
        cfg, where = self.m.config, self.m.secret_status()
        self.vault.setText("" if self.m.secrets.available else "Este computador não tem cofre de credenciais disponível: não é possível salvar senhas nem endereços privados "
                                                               "(instale o pacote “keyring” ou use variáveis de ambiente JARVIS_…). Nada é gravado em arquivo.")
        names = {c.id: c.name for c in cfg.calendars}
        for sid, name, _url, state in self.cal_rows:
            name.setText(names.get(sid, name.text() if sid not in names else ""))
            src = where.get(f"calendar:{sid}", "")
            state.setText("✔ Configurado" + (" (variável de ambiente)" if src == "env" else " (cofre do sistema)") if src else "Não configurado")
        self.email_address.setText(cfg.email_address)
        src = where.get("email", "")
        self.email_state.setText("✔ Senha de aplicativo salva" + (" (variável de ambiente)" if src == "env" else " (cofre do sistema)") if src else "Não configurado")
        self.city.setText(cfg.weather_city)
        self.country.setText(cfg.weather_country)
        self.topics.setText(", ".join(cfg.news_topics))
        self.auto.setChecked(cfg.morning_auto)
        self.speak.setChecked(cfg.morning_speak)
        self.polish.setChecked(cfg.morning_ai_polish)

    def _done(self, result: tuple[bool, str], service_name: str | None) -> None:
        ok, message = result
        self.feedback.setText(("✔ " if ok else "✖ ") + message)
        if ok and service_name:
            self.service.after_config_change(service_name)
        self.reload()

    def _save_calendar(self, sid: str, name: QLineEdit, url: QLineEdit) -> None:
        result = self.m.set_calendar(sid, name.text(), url.text())
        url.clear()  # o endereço nunca fica na tela depois de salvo (nem quando falha)
        self._done(result, "calendar")

    def _remove_calendar(self, sid: str) -> None:
        self._done(self.m.remove_calendar(sid), "calendar")

    def _save_email(self) -> None:
        result = self.m.set_email(self.email_address.text(), self.email_password.text())
        self.email_password.clear()
        self._done(result, "email")

    def _remove_email(self) -> None:
        self.email_password.clear()
        self._done(self.m.remove_email(), "email")

    def _save_weather(self) -> None:
        self._done(self.m.set_weather(self.city.text(), self.country.text()), "weather")

    def _save_news(self) -> None:
        self._done(self.m.set_news_topics(self.topics.text()), "news")

    def _save_morning(self) -> None:
        self._done(self.m.set_morning(self.auto.isChecked(), self.speak.isChecked(), self.polish.isChecked()), None)


class IntegrationsDialog(QDialog):
    def __init__(self, service: IntegrationService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Painéis — agenda, e-mails, notícias e tempo")
        self.resize(760, 640)
        self.setModal(False)
        self.service = service
        lay = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.agenda, self.email, self.news, self.weather = AgendaTab(service), EmailTab(service), NewsTab(service), WeatherTab(service)
        self.settings = SettingsTab(service)
        for tab, title in ((self.agenda, "Agenda"), (self.email, "E-mails"), (self.news, "Notícias"), (self.weather, "Tempo"), (self.settings, "Configurações")):
            self.tabs.addTab(tab, title)
        lay.addWidget(self.tabs)
        self._by_name = {t.name: t for t in (self.agenda, self.email, self.news, self.weather)}
        service.status_changed.connect(self._on_status)
        service.data_changed.connect(self._on_data)
        self.refresh_all_views()

    def refresh_all_views(self) -> None:
        for tab in self._by_name.values():
            tab.update_status()
            tab.reload()
        self.settings.reload()

    def _on_status(self, name: str) -> None:
        if name in self._by_name:
            self._by_name[name].update_status()

    def _on_data(self, name: str) -> None:
        if name in self._by_name:
            self._by_name[name].update_status()
            self._by_name[name].reload()
        self.settings.reload()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt
        self.refresh_all_views()
        super().showEvent(event)

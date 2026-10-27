"""Ponte Qt das integrações: agenda de atualização automática e execução em threads de trabalho (a interface nunca espera a rede).

- Um QTimer (thread de UI) só PERGUNTA o que está vencido; as atualizações rodam num pool pequeno.
- Cada serviço atualiza no máximo uma vez por vez; pedidos repetidos enquanto um está na fila são ignorados.
- Falha numa integração vira estado ("com erro", "em cache") e nunca atrapalha as outras nem a interface.
- Ao encerrar, nada novo começa e as requisições em andamento (todas com timeout) são abandonadas."""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from PySide6.QtCore import QObject, QTimer, Signal

from app.integrations.common import IntegrationError
from app.integrations.manager import SERVICES, IntegrationManager

log = logging.getLogger("jarvis.integrations")


class IntegrationService(QObject):
    status_changed = Signal(str)  # nome do serviço (a interface relê o estado)
    data_changed = Signal(str)  # nome do serviço cujos DADOS mudaram
    morning_ready = Signal(str, bool)  # (texto, falar?)
    email_detail_ready = Signal(str, str, bool)  # (id do e-mail, texto ou erro, ok)
    notice = Signal(str)  # aviso curto para o usuário (fora da conversa)

    def __init__(self, manager: IntegrationManager, parent: QObject | None = None, tick_ms: int = 60_000, first_refresh_ms: int = 2000) -> None:
        super().__init__(parent)
        self.manager = manager
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="jarvis-int")
        self._queued: set[str] = set()
        self._lock = threading.Lock()
        self._stopped = False
        self._auto_morning_done = False
        self._tick_ms, self._first_ms = tick_ms, first_refresh_ms
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    # --- ciclo de vida ---
    def start(self) -> None:
        """Primeira atualização logo depois da janela abrir (nunca antes) e, depois, uma checagem por minuto."""
        QTimer.singleShot(self._first_ms, self._tick)
        self._timer.start(self._tick_ms)

    def shutdown(self) -> None:
        self._stopped = True
        self._timer.stop()
        self.manager.shutdown()
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _submit(self, fn: Callable, *args) -> bool:
        if self._stopped:
            return False
        try:
            self._pool.submit(self._guard, fn, *args)
            return True
        except RuntimeError:  # pool já encerrado
            return False

    def _guard(self, fn: Callable, *args) -> None:
        try:
            fn(*args)
        except Exception:  # noqa: BLE001 - nenhuma tarefa de fundo derruba o aplicativo
            log.exception("[INT] falha inesperada em tarefa de fundo")

    # --- atualização ---
    def _tick(self) -> None:
        self._submit(self._tick_work)

    def _tick_work(self) -> None:
        for name in self.manager.due_services():
            self.refresh(name)

    def refresh(self, name: str) -> None:
        """Pede a atualização de um serviço (manual ou automática). Repetições enquanto já está na fila são ignoradas."""
        with self._lock:
            if name in self._queued:
                return
            self._queued.add(name)
        if not self._submit(self._do_refresh, name):
            with self._lock:
                self._queued.discard(name)

    def refresh_all(self) -> None:
        for name in SERVICES:
            if self.manager.service(name).configured():
                self.refresh(name)

    def _do_refresh(self, name: str) -> None:
        try:
            self.status_changed.emit(name)  # "Atualizando…"
            self.manager.refresh(name)
        finally:
            with self._lock:
                self._queued.discard(name)
            self.status_changed.emit(name)
            self.data_changed.emit(name)

    # --- ações pedidas pela interface ---
    def request_morning(self) -> None:
        self._submit(self._do_morning)

    def _do_morning(self) -> None:
        result = self.manager.morning()
        self.morning_ready.emit(result.text, bool(self.manager.config.morning_speak))

    def maybe_auto_morning(self) -> None:
        """Resumo automático: só se o usuário ligou, uma vez por dia e uma vez por sessão."""
        if self._auto_morning_done:
            return
        self._auto_morning_done = True
        self._submit(lambda: self._do_morning() if self.manager.should_auto_morning() else None)

    def fetch_email_detail(self, email_id: str) -> None:
        def work() -> None:
            try:
                self.email_detail_ready.emit(email_id, self.manager.email_detail(email_id), True)
            except IntegrationError as exc:
                self.email_detail_ready.emit(email_id, exc.message, False)
        self._submit(work)

    def classify_emails(self) -> None:
        def work() -> None:
            if not self.manager.ai_available():
                self.notice.emit("A classificação por IA não está disponível (sem chave da Anthropic ou modo sem IA). Mantive as regras locais.")
                return
            n = self.manager.email.classify_with_ai()
            self.data_changed.emit("email")
            self.notice.emit(f"{n} e-mail(s) reclassificado(s) pela IA." if n else "Nada para reclassificar agora.")
        self._submit(work)

    def open_news(self, url: str) -> None:
        def work() -> None:
            ok, msg = self.manager.open_news(url)
            self.notice.emit(msg)
        self._submit(work)

    def after_config_change(self, name: str) -> None:
        self.status_changed.emit(name)
        self.data_changed.emit(name)
        self.refresh(name)

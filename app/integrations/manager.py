"""Orquestra as quatro integrações e o Resumo da Manhã. Sem Qt: testável sozinho. Seguro entre threads.

- Cada serviço é independente: uma falha nunca atrapalha os outros.
- Quem pede (comando de voz/texto) recebe resposta com o cache; só espera a rede quando o dado está vencido ou não existe.
- Atualizações simultâneas do mesmo serviço não se duplicam (o 2º chamador espera o 1º terminar).
- Mudar credenciais/configuração passa por aqui: validação, cofre (segredos) e settings.json (o resto)."""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone, tzinfo
from typing import Callable
from urllib.parse import urlsplit

from app.core.actions import validate_url
from app.core.integration_intents import IntegrationCommand, IntegrationKind
from app.integrations import answers
from app.integrations.cache import IntegrationCache
from app.integrations.calendar_ics import ALLOWED_HOSTS as CAL_HOSTS, CalendarService, default_local_tz
from app.integrations.common import IntegrationError, ServiceState, ServiceStatus, clean_text
from app.integrations.config import MAX_CALENDARS, MAX_TOPICS, CalendarSlot, IntegrationsConfig
from app.integrations.gmail import SECRET_PASSWORD, EmailService
from app.integrations.morning import MorningResult, compose
from app.integrations.news import NewsService
from app.integrations.secrets import SecretStore, SecretStoreUnavailable
from app.integrations.weather import WeatherService

log = logging.getLogger("jarvis.integrations")
SERVICES = ("calendar", "email", "news", "weather")
LABELS = {"calendar": "Agenda", "email": "E-mails", "news": "Notícias", "weather": "Tempo"}
_EMAIL = re.compile(r"[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}")


@dataclass(frozen=True)
class IntegrationReply:
    text: str
    ok: bool = True


class IntegrationManager:
    def __init__(self, cfg: IntegrationsConfig, save_cfg: Callable[[IntegrationsConfig], None], secrets: SecretStore, cache: IntegrationCache, *,
                 fetch_ics=None, fetch_rss=None, fetch_json=None, imap_factory=None, ai=None, ai_allowed: Callable[[], bool] = lambda: True,
                 memory=None, clock: Callable[[], datetime] | None = None, local_tz: tzinfo | None = None,
                 url_opener: Callable[[str], bool] | None = None, network_allowed: Callable[[], bool] = lambda: True, metrics=None) -> None:
        self._config, self._save, self.secrets, self.cache = cfg, save_cfg, secrets, cache
        self._network_allowed, self.metrics = network_allowed, metrics  # modo Offline: só dados salvos, nenhuma consulta à internet
        self._cfg_lock = threading.Lock()
        self.ai, self._ai_allowed, self.memory, self._open_url = ai, ai_allowed, memory, url_opener
        self.local_tz = local_tz or default_local_tz()
        self._clock = clock or (lambda: datetime.now(self.local_tz))
        provider = lambda: self._config  # noqa: E731 - os serviços sempre leem a configuração ATUAL
        utc_clock = lambda: self._clock().astimezone(timezone.utc)  # noqa: E731
        cache.clock = utc_clock
        self.calendar = CalendarService(provider, secrets, cache, fetch_ics, self._clock, self.local_tz)
        self.email = EmailService(provider, secrets, cache, imap_factory, utc_clock, (lambda s, u: self._ai_complete(s, u, "email_ai")) if ai is not None else None)
        self.news = NewsService(provider, cache, fetch_rss, utc_clock)
        self.weather = WeatherService(provider, cache, fetch_json, utc_clock)
        self._stop = threading.Event()
        self._morning_lock = threading.Lock()
        self._morning_last: tuple[float, MorningResult] | None = None

    # --- configuração atual ---
    @property
    def config(self) -> IntegrationsConfig:
        return self._config

    def service(self, name: str):
        return {"calendar": self.calendar, "email": self.email, "news": self.news, "weather": self.weather}[name]

    def now(self) -> datetime:
        return self._clock().astimezone(self.local_tz)

    # --- IA (opcional; só quando permitida pelo modo e configurada) ---
    def ai_available(self) -> bool:
        return self.ai is not None and self._ai_allowed() and bool(getattr(self.ai, "is_configured", False))

    def _ai_complete(self, system: str, user: str, purpose: str = "extra") -> str:
        """Chamada de IA das integrações (classificar e-mails, reescrever o resumo). Entra nas métricas como metadado (sem texto)."""
        if not self.ai_available():
            raise IntegrationError("unavailable", "IA indisponível.")
        try:
            result = self.ai.generate(system, [{"role": "user", "content": user}])
        except Exception as exc:
            self._metric(purpose, False, None, getattr(exc, "kind", type(exc).__name__))
            raise
        self._metric(purpose, True, result)
        return result.text

    def _metric(self, purpose: str, ok: bool, result, error: str | None = None) -> None:
        if self.metrics is None:
            return
        try:
            tok = lambda name: getattr(result, name, None) if isinstance(getattr(result, name, None), int) else None  # noqa: E731
            self.metrics.record(submission_id=None, kind="ai", ok=ok, source=purpose, model=getattr(result, "model", None) if isinstance(getattr(result, "model", None), str) else None,
                                input_tokens=tok("input_tokens"), output_tokens=tok("output_tokens"),
                                duration_s=getattr(result, "duration_s", None) if isinstance(getattr(result, "duration_s", None), (int, float)) else None, error_kind=error)
        except Exception:  # noqa: BLE001 - métrica nunca atrapalha
            log.exception("[INT] falha ao registrar métrica")

    # --- atualização ---
    def status(self, name: str) -> ServiceStatus:
        return self.service(name).status()

    def statuses(self) -> dict[str, ServiceStatus]:
        return {n: self.status(n) for n in SERVICES}

    def due_services(self) -> list[str]:
        return [] if self._stop.is_set() or not self._network_allowed() else [n for n in SERVICES if self.service(n).configured() and self.service(n).due()]

    def refresh(self, name: str) -> ServiceStatus:
        """Atualiza um serviço (bloqueia só quem chama; use numa thread de trabalho). Se já está atualizando, não repete."""
        if self._stop.is_set() or not self._network_allowed():
            return self.status(name)
        svc = self.service(name)
        if name == "email" and svc.auth_blocked():
            self.cache.clear_state("email")  # atualização MANUAL libera de novo depois de um login recusado
        return svc.refresh()

    def ensure_fresh(self, name: str, force: bool = False) -> None:
        """Atualiza se estiver vencido (ou `force`). Se outra thread já está atualizando, espera até ~10 s por ela em vez de repetir a requisição."""
        svc = self.service(name)
        if self._stop.is_set() or not svc.configured() or not self._network_allowed():
            return
        for _ in range(200):
            if not svc.busy():
                break
            time.sleep(0.05)
            force = False  # quem estava atualizando já trouxe dado novo
        else:
            return
        if force or svc.due():
            svc.refresh()

    # --- respostas a comandos ---
    def answer(self, cmd: IntegrationCommand) -> IntegrationReply:
        reply = self._answer(cmd)
        if not self._network_allowed() and reply.ok:
            reply = IntegrationReply(reply.text + " (Modo offline: só dados salvos, sem atualização.)", reply.ok)
        return reply

    def _answer(self, cmd: IntegrationCommand) -> IntegrationReply:
        now = self.now()
        try:
            if cmd.kind is IntegrationKind.AGENDA:
                self.ensure_fresh("calendar")
                return IntegrationReply(answers.agenda_text(self.calendar, cmd.period or "today", now))
            if cmd.kind is IntegrationKind.EMAIL:
                if self.email.configured():
                    self.ensure_fresh("email", force=self.email.last_ok() is None or self._email_age_s() > 120)
                    if cmd.mode in ("attention", "check") and self.ai_available() and self.email.ai_pending():
                        self.email.classify_with_ai()  # 1 chamada em lote, só com o que ficou em dúvida e só porque você perguntou
                return IntegrationReply(answers.email_text(self.email, cmd.mode or "check", now))
            if cmd.kind is IntegrationKind.NEWS:
                self.ensure_fresh("news")
                return IntegrationReply(answers.news_text(self.news, cmd.topic, now, cmd.detail))
            if cmd.kind is IntegrationKind.WEATHER:
                self.ensure_fresh("weather")
                return IntegrationReply(answers.weather_text(self.weather, cmd.when or "today", cmd.rain, now))
            if cmd.kind is IntegrationKind.MORNING:
                return IntegrationReply(self.morning().text)
        except Exception:  # noqa: BLE001 - uma integração com defeito nunca derruba o assistente
            log.exception("[INT] falha inesperada ao responder %s", cmd.kind.name)
            return IntegrationReply("Não consegui consultar isso agora. O restante do Jarvis continua funcionando.", ok=False)
        return IntegrationReply("Não entendi esse pedido.", ok=False)

    def _email_age_s(self) -> float:
        e = self.email.cache.get("email", "inbox")
        return (self._clock().astimezone(timezone.utc) - e.fetched_at).total_seconds() if e else float("inf")

    # --- Resumo da Manhã ---
    def morning(self, fresh: bool = False) -> MorningResult:
        """Uma montagem por vez: pedidos simultâneos (clique duplo, voz + botão) esperam e recebem o MESMO resultado.
        Pedidos em até 15 s reaproveitam o resultado (evita trabalho e chamadas à IA repetidos); `fresh=True` não reaproveita."""
        with self._morning_lock:
            if not fresh and self._morning_last and time.monotonic() - self._morning_last[0] < 15:
                return self._morning_last[1]
            polish = (lambda s, u: self._ai_complete(s, u, "morning_ai")) if (self._config.morning_ai_polish and self.ai_available()) else None
            result = compose(self, self.now(), polish)
            self._morning_last = (time.monotonic(), result)
            self.cache.put("morning", "last_run", {"date": self.now().date().isoformat()})
            return result

    def should_auto_morning(self) -> bool:
        if not self._config.morning_auto:
            return False
        e = self.cache.get("morning", "last_run")
        return not (e and e.payload.get("date") == self.now().date().isoformat())

    # --- mudanças de configuração (validação, cofre e settings.json) ---
    def _update(self, **changes) -> None:
        with self._cfg_lock:
            self._config = replace(self._config, **changes)
            self._morning_last = None  # mudou uma preferência: o resumo reaproveitado já não vale
            self._save(self._config)

    def set_calendar(self, slot_id: str, name: str, url: str) -> tuple[bool, str]:
        url = (url or "").strip()
        checked, why = validate_url(url)
        parts = urlsplit(checked) if checked else None
        if why or parts is None or parts.scheme != "https" or (parts.hostname or "").lower() not in CAL_HOSTS:
            return False, "Cole o endereço iCal secreto do Google Calendar (começa com https://calendar.google.com/…)."
        existing = {c.id for c in self._config.calendars}
        if slot_id not in existing and len(existing) >= MAX_CALENDARS:
            return False, f"Por enquanto a interface aceita até {MAX_CALENDARS} calendários."
        try:
            self.secrets.set(self.calendar.secret_name(slot_id), url)
        except SecretStoreUnavailable as exc:
            return False, str(exc)
        slots = [c for c in self._config.calendars if c.id != slot_id] + [CalendarSlot(slot_id, clean_text(name, 40) or slot_id)]
        self._update(calendars=tuple(sorted(slots, key=lambda c: c.id)))
        self.cache.delete("calendar", f"cal:{slot_id}")
        self.cache.clear_state("calendar")
        return True, "Calendário salvo com segurança. Atualizando…"

    def remove_calendar(self, slot_id: str) -> tuple[bool, str]:
        self.secrets.delete(self.calendar.secret_name(slot_id))
        self.cache.delete("calendar", f"cal:{slot_id}")
        self._update(calendars=tuple(c for c in self._config.calendars if c.id != slot_id))
        return True, "Calendário removido (endereço e dados salvos apagados)."

    def set_email(self, address: str, app_password: str) -> tuple[bool, str]:
        address, pw = (address or "").strip(), re.sub(r"\s+", "", app_password or "")  # o Google mostra a senha em blocos com espaços
        if not _EMAIL.fullmatch(address):
            return False, "Informe o endereço de e-mail completo."
        if not pw:
            if self.secrets.get(SECRET_PASSWORD) and address:
                self._update(email_address=address)  # só trocou o endereço; a senha já salva continua
                return True, "Endereço atualizado."
            return False, "Informe a senha de aplicativo do Google (não a senha normal da conta)."
        if len(pw) != 16 or not (pw.isascii() and pw.isalnum()):
            return False, "A senha de aplicativo do Google tem exatamente 16 letras, sem símbolos. Gere uma em Conta Google > Segurança > Senhas de app (não use a senha normal)."
        try:
            self.secrets.set(SECRET_PASSWORD, pw)
        except SecretStoreUnavailable as exc:
            return False, str(exc)
        self._update(email_address=address)
        self.cache.delete("email")
        self.cache.clear_state("email")
        return True, "Conta salva com segurança. Atualizando…"

    def remove_email(self) -> tuple[bool, str]:
        self.secrets.delete(SECRET_PASSWORD)
        self.cache.delete("email")
        self.cache.clear_state("email")
        self._update(email_address="")
        return True, "Conta removida (senha e e-mails salvos apagados)."

    def set_weather(self, city: str, country: str) -> tuple[bool, str]:
        city, country = clean_text(city, 80), (country or "").strip().upper()
        if not city:
            self._update(weather_city="", weather_country="")
            self.cache.delete("weather")
            return True, "Localização removida."
        if country and not (len(country) == 2 and country.isalpha()):
            return False, "O país é opcional; se informar, use o código de 2 letras (ex.: BR)."
        self._update(weather_city=city, weather_country=country)
        self.cache.delete("weather", "forecast")
        self.cache.clear_state("weather")
        return True, "Localização salva. Atualizando…"

    def set_news_topics(self, raw: str) -> tuple[bool, str]:
        topics = list(dict.fromkeys(t for t in (clean_text(x, 40) for x in re.split(r"[,;\n]", raw or "")) if t))[:MAX_TOPICS]
        if not topics:
            return False, "Informe pelo menos um assunto (separe por vírgula)."
        self._update(news_topics=tuple(topics))
        return True, "Assuntos salvos. Atualizando…"

    def set_morning(self, auto: bool, speak: bool, ai_polish: bool) -> tuple[bool, str]:
        self._update(morning_auto=bool(auto), morning_speak=bool(speak), morning_ai_polish=bool(ai_polish))
        return True, "Preferências do resumo salvas."

    # --- ações do usuário na interface ---
    def email_detail(self, email_id: str) -> str:
        return self.email.detail(email_id)

    def open_news(self, url: str) -> tuple[bool, str]:
        """Abre uma notícia no navegador padrão, SÓ por ação do usuário e só se o endereço passar na validação (http/https)."""
        checked, why = validate_url(url)
        if why or self._open_url is None:
            return False, "Não abri: o endereço não passou na validação." if why else "Não há navegador disponível para abrir o endereço."
        return (True, "Abrindo a notícia no navegador.") if self._open_url(checked) else (False, "O navegador não aceitou abrir o endereço.")

    def secret_status(self) -> dict[str, str]:
        """Só diz ONDE está cada credencial (nunca o valor): "keyring" | "env" | ""."""
        out = {"email": self.secrets.source(SECRET_PASSWORD) or ""}
        for c in self._config.calendars:
            out[f"calendar:{c.id}"] = self.secrets.source(self.calendar.secret_name(c.id)) or ""
        return out

    def shutdown(self) -> None:
        self._stop.set()

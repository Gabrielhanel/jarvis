"""Texto das respostas (curto, em português, pensado para ler na tela e ouvir). Funções puras sobre o que os serviços já têm em cache.
Regras: nunca inventar; separar "vazio" de "falha"; avisar quando os dados são antigos; falar pouco (até ~6 itens)."""
from __future__ import annotations

from datetime import datetime, timedelta

from app.integrations.calendar_ics import CalendarService, CalEvent
from app.integrations.common import ServiceState, ServiceStatus
from app.integrations.gmail import ACTION, INFO, NOISE, EmailService
from app.integrations.news import NewsService
from app.integrations.weather import WeatherService, describe_code, fmt_t

DAYS = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]
MAX_ITEMS = 6
CAL_UNCONFIGURED = "O Google Calendar ainda não está configurado. Abra Painéis, vá em Configurações e cole o endereço iCal secreto do calendário."
EMAIL_UNCONFIGURED = "O Gmail ainda não está configurado. Abra Painéis, vá em Configurações e informe seu endereço e uma senha de aplicativo do Google."
WEATHER_UNCONFIGURED = "Informe sua cidade em Painéis, Configurações, para eu consultar o clima."


def when_text(dt: datetime, now: datetime) -> str:
    dt = dt.astimezone(now.tzinfo)
    return dt.strftime("%H:%M") if dt.date() == now.date() else dt.strftime("%d/%m %H:%M")


def data_note(st: ServiceStatus, now: datetime) -> str:
    """Aviso curto quando o que está sendo mostrado não é de uma atualização bem-sucedida agora."""
    if st.state not in (ServiceState.CACHED, ServiceState.ERROR) or st.last_ok is None:
        return ""
    return f" (dados de {when_text(st.last_ok, now)}; " + ("não consegui atualizar agora)" if st.error_kind else "estão desatualizados)")


def no_data(label: str, st: ServiceStatus) -> str:
    return f"Não consegui acessar {label} agora ({st.message.rstrip('.').lower() or 'falha na conexão'}) e ainda não há dados salvos."


def hm(ev: CalEvent, now: datetime, day0: datetime | None = None) -> str:
    """Horário para falar: "dia inteiro", "14:30" ou, se o evento começou ANTES deste dia, "até 02:00"."""
    if ev.all_day:
        return "dia inteiro"
    if day0 is not None and ev.start < day0:
        return "até " + ev.end.astimezone(now.tzinfo).strftime("%H:%M")
    return ev.start.astimezone(now.tzinfo).strftime("%H:%M")


def _ev_line(ev: CalEvent, now: datetime, multi: bool, day0: datetime | None = None) -> str:
    return f"{hm(ev, now, day0)} {ev.title}" + (f" ({' e '.join(ev.calendars)})" if multi else "")


def _limited(lines: list[str], sep: str = "; ") -> str:
    shown = lines[:MAX_ITEMS]
    return sep.join(shown) + (f"; e mais {len(lines) - MAX_ITEMS}" if len(lines) > MAX_ITEMS else "")


def day_order(events: list[CalEvent]) -> list[CalEvent]:
    """Dentro de um dia: eventos de dia inteiro primeiro, depois por horário."""
    return sorted(events, key=lambda e: (not e.all_day, e.start))


def _day_start(now: datetime, offset: int = 0) -> datetime:
    return datetime.combine(now.date() + timedelta(days=offset), datetime.min.time(), tzinfo=now.tzinfo)


def agenda_text(cal: CalendarService, period: str, now: datetime) -> str:
    st = cal.status()
    if st.state is ServiceState.UNCONFIGURED:
        return CAL_UNCONFIGURED
    if cal.last_ok() is None:
        return no_data("a agenda", st)  # falha SEM dados: diferente de agenda vazia
    note = data_note(st, now) + _notes(cal.notes())
    multi = len(cal.slots()) > 1
    if period == "week":
        evs = cal.events(_day_start(now), _day_start(now, 8))
        if not evs:
            return "Você não tem compromissos nos próximos 7 dias." + note
        lines = []
        for off in range(8):
            d0, d1 = _day_start(now, off), _day_start(now, off + 1)
            day = day_order([e for e in evs if e.start < d1 and e.end > d0 or e.start == d0])
            if day:
                name = "Hoje" if off == 0 else "Amanhã" if off == 1 else f"{DAYS[d0.weekday()].capitalize()} {d0:%d/%m}"
                lines.append(f"{name}: " + _limited([_ev_line(e, now, multi, d0) for e in day], "; "))
        return "Sua agenda dos próximos dias:\n" + "\n".join(lines) + note
    off = 1 if period == "tomorrow" else 0
    d0, d1 = _day_start(now, off), _day_start(now, off + 1)
    evs = day_order([e for e in cal.events(d0, d1) if e.start < d1 and (e.end > d0 or e.start == d0)])
    label = "amanhã" if off else "hoje"
    if not evs:
        return f"Você não tem compromissos {label}." + note
    if off == 0:  # hoje: prioriza o que ainda vem pela frente
        upcoming = [e for e in evs if e.all_day or e.end > now or e.start >= now]
        past = len(evs) - len(upcoming)
        if not upcoming:
            return f"Você não tem mais compromissos hoje ({_passed(past)})." + note
        n = len(upcoming)
        extra = f" ({_passed(past)})" if past else ""
        return f"Hoje você ainda tem {n} compromisso{'s' if n > 1 else ''}{extra}: " + _limited([_ev_line(e, now, multi, d0) for e in upcoming]) + "." + note
    return f"Amanhã você tem {len(evs)} compromisso{'s' if len(evs) > 1 else ''}: " + _limited([_ev_line(e, now, multi, d0) for e in evs]) + "." + note


def _ask(n: int) -> str:
    return f"{n} pede ação" if n == 1 else f"{n} pedem ação"


def _passed(n: int) -> str:
    return f"{n} já aconteceu" if n == 1 else f"{n} já aconteceram"


def _notes(notes: list[str]) -> str:
    return (" Aviso: " + "; ".join(notes) + ".") if notes else ""


def email_text(svc: EmailService, mode: str, now: datetime) -> str:
    st = svc.status()
    if st.state is ServiceState.UNCONFIGURED:
        return EMAIL_UNCONFIGURED
    if svc.last_ok() is None:
        return no_data("o Gmail", st)
    items, counts = svc.items(), svc.counts()
    note = data_note(st, now)
    simp = " A classificação é simplificada (regras locais, sem IA)." if svc.simplified() else ""
    if not items:
        return "Sua caixa de entrada não tem mensagens recentes." + note

    def line(i, reason=False):
        return f"{i.subject}, de {i.sender}" + (f" ({i.reason.lower()})" if reason else "")

    if mode == "attention":
        a = svc.items(ACTION)
        if not a:
            return "Nenhum dos e-mails recentes parece exigir ação." + note + simp
        return f"{len(a)} e-mail{'s' if len(a) > 1 else ''} para sua atenção: " + _limited([line(i, True) for i in a]) + "." + note + simp
    if mode == "info":
        inf = svc.items(INFO)
        return ("Nenhum e-mail informativo entre os recentes." if not inf else f"{len(inf)} informativo{'s' if len(inf) > 1 else ''}: " + _limited([line(i) for i in inf]) + ".") + note + simp
    if mode == "noise":
        nz = svc.items(NOISE)
        senders = list(dict.fromkeys(i.sender for i in nz))[:3]
        return ("Nenhum e-mail de ruído entre os recentes." if not nz else f"{len(nz)} de ruído, principalmente de {', '.join(senders)}.") + note + simp
    unread = sum(1 for i in items if i.unread)
    text = f"Nas suas {len(items)} mensagens mais recentes: {_ask(counts[ACTION])}, {counts[INFO]} {'é informativa' if counts[INFO] == 1 else 'são informativas'} e {counts[NOISE]} {'é ruído' if counts[NOISE] == 1 else 'são ruído'}"
    text += f"; {unread} não lidas." if unread else "."
    a = svc.items(ACTION)[:3]
    if a:
        text += " Precisam de atenção: " + "; ".join(line(i) for i in a) + "."
    return text + note + simp


def _news_line(n, idx: int) -> str:
    return f"{idx}) {n.title} ({n.source})"


def news_text(svc: NewsService, topic: str, now: datetime, detail: bool = False) -> str:
    st = svc.status()
    if svc.last_ok() is None:
        return no_data("as notícias", st)
    note = data_note(st, now)
    if not topic:
        heads = svc.headlines(3)
        if not heads:
            return "Não há notícias salvas no momento." + note
        return "Principais manchetes: " + " ".join(_news_line(n, i) for i, n in enumerate(heads, 1)) + note
    found = svc.find_topic(topic)
    if found is None:
        return f"Não acompanho o assunto “{topic}”. Acompanho: {', '.join(svc.topics())}. Dá para mudar em Painéis, Configurações."
    items = svc.by_topic().get(found, [])
    if not items:
        return f"Não há notícias salvas sobre {found} no momento." + note
    top = items[:5 if detail else 3]
    return f"{found.capitalize()}: " + " ".join(_news_line(n, i) for i, n in enumerate(top, 1)) + note


def weather_text(svc: WeatherService, when: str, rain: bool, now: datetime) -> str:
    st = svc.status()
    if st.state is ServiceState.UNCONFIGURED:
        return WEATHER_UNCONFIGURED
    snap = svc.snapshot()
    if snap is None:
        return no_data("a previsão do tempo", st)
    data, fetched = snap
    note = data_note(st, now)
    place = data.get("place") or "sua cidade"
    off = 1 if when == "tomorrow" else 0
    day, old = svc.day(data, off)
    cur = data.get("current") or {}
    if day is None:
        return f"Não tenho a previsão {'de amanhã' if off else 'de hoje'} para {place}: os dados salvos são de {when_text(fetched, now)}." + note
    pop, psum = day.get("pop"), day.get("psum")
    if rain:
        if off == 0 and (cur.get("precip") or 0) > 0:
            return f"Está chovendo agora em {place}." + (f" Chance de chuva hoje: {round(pop)}%." if pop is not None else "") + note
        if pop is None:
            return f"Não tenho a probabilidade de chuva {'de amanhã' if off else 'de hoje'} para {place}." + note
        verdict = "Sim, provavelmente vai chover" if pop >= 60 else "Pode chover" if pop >= 30 else "É improvável que chova"
        return f"{verdict} {'amanhã' if off else 'hoje'} em {place}: chance de {round(pop)}%." + note
    parts = []
    if off == 0 and when != "today" and cur:
        now_bits = f"agora {fmt_t(cur.get('temp'))}" + (f" (sensação {fmt_t(cur.get('feels'))})" if cur.get("feels") is not None else "")
        parts.append(f"{now_bits}, {describe_code(cur.get('code'))}")
    elif off == 0 and cur:
        parts.append(f"agora {fmt_t(cur.get('temp'))}, {describe_code(cur.get('code'))}")
    range_ = []
    if day.get("tmin") is not None:
        range_.append(f"mínima {fmt_t(day['tmin'])}")
    if day.get("tmax") is not None:
        range_.append(f"máxima {fmt_t(day['tmax'])}")
    cond = describe_code(day.get("code"))
    head = f"{'Amanhã' if off else 'Hoje'}: {cond}" + (", " + ", ".join(range_) if range_ else "")
    if pop is not None:
        head += f", chance de chuva {round(pop)}%"
    prefix = f"Em {place}: " + ("; ".join(parts) + ". " if parts else "")
    return prefix + head + "." + note

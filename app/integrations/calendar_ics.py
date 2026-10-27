"""Google Calendar por feed privado iCal/ICS (SOMENTE LEITURA).

- Cada endereço privado é uma CREDENCIAL: vive no cofre, nunca em log, mensagem de erro ou arquivo de configuração.
- O ICS bruto não é guardado: só os eventos já interpretados da janela (hoje + próximos dias) vão para o cache.
- Interpretação própria e enxuta (a biblioteca padrão não tem iCal): fusos IANA, dia inteiro, eventos que cruzam a meia-noite,
  RRULE comum (DAILY/WEEKLY/MONTHLY/YEARLY com INTERVAL, COUNT, UNTIL, BYDAY, BYMONTHDAY), EXDATE e RECURRENCE-ID.
  Regras que não entendemos (BYSETPOS, BYWEEKNO...) NÃO são adivinhadas: o evento aparece só na data inicial e a agenda avisa."""
from __future__ import annotations

import calendar as _cal
import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone, tzinfo
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.integrations.cache import IntegrationCache, utcnow
from app.integrations.common import IntegrationError, ServiceState, ServiceStatus, clean_text, http_get, norm_key, with_retries
from app.integrations.config import IntegrationsConfig
from app.integrations.secrets import SecretStore

log = logging.getLogger("jarvis.integrations")
ALLOWED_HOSTS = frozenset({"calendar.google.com"})
MAX_ICS_BYTES = 8_000_000
_WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
_UNSUPPORTED_RULE_KEYS = {"BYSETPOS", "BYWEEKNO", "BYYEARDAY", "BYHOUR", "BYMINUTE", "BYSECOND"}


@dataclass(frozen=True)
class CalEvent:
    uid: str
    title: str
    start: datetime  # com fuso local; em evento de dia inteiro, 00:00 do primeiro dia
    end: datetime  # exclusivo em evento de dia inteiro
    all_day: bool
    calendars: tuple[str, ...]  # nomes amigáveis (mais de um quando o mesmo evento está em dois calendários)

    def to_dict(self) -> dict:
        return {"uid": self.uid, "title": self.title, "start": self.start.isoformat(), "end": self.end.isoformat(), "all_day": self.all_day,
                "calendars": list(self.calendars)}

    @staticmethod
    def from_dict(d: dict) -> "CalEvent":
        return CalEvent(str(d["uid"]), str(d["title"]), datetime.fromisoformat(d["start"]), datetime.fromisoformat(d["end"]), bool(d["all_day"]),
                        tuple(str(x) for x in d["calendars"]))


# --- leitura do formato ----------------------------------------------------------------------------------------------
def unfold(text: str) -> list[str]:
    out: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def split_prop(line: str) -> tuple[str, dict[str, str], str] | None:
    """NOME;PARAM=valor;PARAM="a:b":valor  ->  (NOME, {PARAM: valor}, valor). O ':' dentro de aspas não separa."""
    in_quotes, colon = False, -1
    for i, ch in enumerate(line):
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == ":" and not in_quotes:
            colon = i
            break
    if colon < 1:
        return None
    head, value = line[:colon], line[colon + 1:]
    parts, buf, q = [], "", False
    for ch in head:
        if ch == '"':
            q = not q
        if ch == ";" and not q:
            parts.append(buf)
            buf = ""
        else:
            buf += ch
    parts.append(buf)
    params = {}
    for p in parts[1:]:
        k, _, v = p.partition("=")
        params[k.upper()] = v.strip('"')
    return parts[0].upper(), params, value


def unescape(value: str) -> str:
    return re.sub(r"\\([nN,;\\])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), value)


class _Ctx:
    def __init__(self, local_tz: tzinfo, default_tz: tzinfo | None) -> None:
        self.local_tz, self.default_tz, self.bad_tz = local_tz, default_tz, 0

    def zone(self, name: str | None) -> tzinfo:
        if name:
            try:
                return ZoneInfo(name)
            except (ZoneInfoNotFoundError, ValueError, OSError):
                self.bad_tz += 1  # fuso desconhecido (ex.: nome do Windows, ou falta o pacote tzdata): usa o do calendário/local
        return self.default_tz or self.local_tz


def parse_dt(value: str, params: dict[str, str], ctx: _Ctx) -> date | datetime | None:
    v = value.strip()
    try:
        if params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", v):
            return datetime.strptime(v[:8], "%Y%m%d").date()
        if re.fullmatch(r"\d{8}T\d{6}Z", v):
            return datetime.strptime(v, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        if re.fullmatch(r"\d{8}T\d{6}", v):
            return datetime.strptime(v, "%Y%m%dT%H%M%S").replace(tzinfo=ctx.zone(params.get("TZID")))
        if re.fullmatch(r"\d{8}T\d{4}", v):
            return datetime.strptime(v, "%Y%m%dT%H%M").replace(tzinfo=ctx.zone(params.get("TZID")))
    except ValueError:
        return None
    return None


def parse_duration(value: str) -> timedelta | None:
    m = re.fullmatch(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", value.strip())
    if not m or not any(m.groups()[1:]):
        return None
    sign = -1 if m.group(1) == "-" else 1
    w, d, h, mi, s = (int(x or 0) for x in m.groups()[1:])
    return sign * timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=s)


@dataclass
class _Raw:
    uid: str = ""
    title: str = ""
    start: date | datetime | None = None
    end: date | datetime | None = None
    duration: timedelta | None = None
    rrule: str = ""
    exdates: list[date | datetime] = field(default_factory=list)
    recurrence_id: date | datetime | None = None
    cancelled: bool = False


def parse_events(text: str, ctx: _Ctx) -> list[_Raw]:
    events: list[_Raw] = []
    cur: _Raw | None = None
    depth = 0  # componentes aninhados (ex.: VALARM) dentro do VEVENT são ignorados
    for line in unfold(text):
        prop = split_prop(line.strip("\ufeff")) if line.strip() else None
        if prop is None:
            continue
        name, params, value = prop
        if name == "BEGIN":
            if value.upper() == "VEVENT" and cur is None:
                cur, depth = _Raw(), 0
            elif cur is not None:
                depth += 1
            continue
        if name == "END":
            if cur is not None and depth > 0:
                depth -= 1
            elif cur is not None and value.upper() == "VEVENT":
                if cur.start is not None:
                    events.append(cur)
                cur = None
            continue
        if cur is None or depth > 0:
            continue
        if name == "UID":
            cur.uid = value.strip()
        elif name == "SUMMARY":
            cur.title = unescape(value)
        elif name == "DTSTART":
            cur.start = parse_dt(value, params, ctx)
        elif name == "DTEND":
            cur.end = parse_dt(value, params, ctx)
        elif name == "DURATION":
            cur.duration = parse_duration(value)
        elif name == "RRULE":
            cur.rrule = value.strip()
        elif name == "EXDATE":
            for part in value.split(","):
                d = parse_dt(part, params, ctx)
                if d is not None:
                    cur.exdates.append(d)
        elif name == "RECURRENCE-ID":
            cur.recurrence_id = parse_dt(value, params, ctx)
        elif name == "STATUS" and value.strip().upper() == "CANCELLED":
            cur.cancelled = True
    return events


# --- recorrência ------------------------------------------------------------------------------------------------------
def _parse_rule(rule: str) -> tuple[dict[str, str], bool]:
    parts = dict(p.split("=", 1) for p in rule.split(";") if "=" in p)
    parts = {k.upper(): v for k, v in parts.items()}
    ok = parts.get("FREQ") in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY") and not (set(parts) & _UNSUPPORTED_RULE_KEYS)
    return parts, ok


def _byday(spec: str | None) -> list[tuple[int | None, int]]:
    out = []
    for item in (spec or "").split(","):
        m = re.fullmatch(r"([+-]?\d+)?(MO|TU|WE|TH|FR|SA|SU)", item.strip().upper())
        if m:
            out.append((int(m.group(1)) if m.group(1) else None, _WEEKDAYS[m.group(2)]))
    return out


def _nth_weekday(year: int, month: int, n: int, weekday: int) -> date | None:
    days = [date(year, month, d) for d in range(1, _cal.monthrange(year, month)[1] + 1) if date(year, month, d).weekday() == weekday]
    try:
        return days[n - 1] if n > 0 else days[n]
    except IndexError:
        return None


def expand_rule(rule: str, first: date | datetime, horizon: date, max_iter: int = 60_000) -> tuple[list[date | datetime], bool]:
    """Datas/horas de cada ocorrência (a partir da primeira) até `horizon`. Devolve (ocorrências, regra_entendida).
    A aritmética é no relógio local do evento (a hora de parede se mantém em horário de verão)."""
    parts, ok = _parse_rule(rule)
    if not ok:
        return [first], False
    is_dt = isinstance(first, datetime)
    tzi = first.tzinfo if is_dt else None
    base = first.replace(tzinfo=None) if is_dt else first
    first_day = base.date() if is_dt else base
    tod = base.time() if is_dt else None
    freq = parts["FREQ"]
    interval = max(1, int(parts.get("INTERVAL", "1") or 1)) if parts.get("INTERVAL", "1").isdigit() else 1
    count = int(parts["COUNT"]) if parts.get("COUNT", "").isdigit() else None
    until: date | datetime | None = None
    if parts.get("UNTIL"):
        u = parse_dt(parts["UNTIL"], {}, _Ctx(timezone.utc, None))
        until = u
    byday = _byday(parts.get("BYDAY"))
    bymonthday = [int(x) for x in parts.get("BYMONTHDAY", "").split(",") if re.fullmatch(r"-?\d+", x)]
    bymonth = [int(x) for x in parts.get("BYMONTH", "").split(",") if x.isdigit()]

    def build(day: date):
        return datetime.combine(day, tod).replace(tzinfo=tzi) if is_dt else day

    def past_until(day: date) -> bool:
        if until is None:
            return False
        occ = build(day)
        if isinstance(until, datetime) and isinstance(occ, datetime):
            return occ.astimezone(timezone.utc) > until.astimezone(timezone.utc)
        return day > (until.date() if isinstance(until, datetime) else until)

    def candidates():
        if freq == "DAILY":
            d = first_day
            while True:
                if not byday or d.weekday() in {w for _, w in byday}:
                    yield d
                d += timedelta(days=interval)
        elif freq == "WEEKLY":
            wk = first_day - timedelta(days=first_day.weekday())
            days = sorted({w for _, w in byday}) or [first_day.weekday()]
            while True:
                for w in days:
                    yield wk + timedelta(days=w)
                wk += timedelta(weeks=interval)
        elif freq == "MONTHLY":
            y, m = first_day.year, first_day.month
            while True:
                found: list[date] = []
                if bymonthday:
                    last = _cal.monthrange(y, m)[1]
                    found = [date(y, m, d if d > 0 else last + 1 + d) for d in bymonthday if 1 <= (d if d > 0 else last + 1 + d) <= last]
                elif byday:
                    for n, w in byday:
                        if n is None:
                            found += [date(y, m, d) for d in range(1, _cal.monthrange(y, m)[1] + 1) if date(y, m, d).weekday() == w]
                        else:
                            x = _nth_weekday(y, m, n, w)
                            if x:
                                found.append(x)
                elif first_day.day <= _cal.monthrange(y, m)[1]:
                    found = [date(y, m, first_day.day)]
                yield from sorted(found)
                m += interval
                y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
        else:  # YEARLY
            y = first_day.year
            while True:
                months = bymonth or [first_day.month]
                for m in months:
                    last = _cal.monthrange(y, m)[1]
                    if bymonthday:
                        for d in sorted(bymonthday):
                            dd = d if d > 0 else last + 1 + d
                            if 1 <= dd <= last:
                                yield date(y, m, dd)
                    elif byday:
                        for n, w in byday:
                            x = _nth_weekday(y, m, n or 1, w)
                            if x:
                                yield x
                    elif first_day.day <= last:
                        yield date(y, m, first_day.day)
                y += interval

    out: list[date | datetime] = []
    emitted = 0
    for i, day in enumerate(candidates()):
        if i > max_iter:
            return out, False
        if day < first_day:
            continue
        if day > horizon or past_until(day):
            break
        emitted += 1
        out.append(build(day))
        if count is not None and emitted >= count:
            break
    return out, True


# --- janela de consulta ---------------------------------------------------------------------------------------------------
def _to_local(value: date | datetime, local_tz: tzinfo) -> tuple[datetime, bool]:
    if isinstance(value, datetime):
        return value.astimezone(local_tz), False
    return datetime.combine(value, dtime.min, tzinfo=local_tz), True


def _same_instant(a: date | datetime, b: date | datetime) -> bool:
    if isinstance(a, datetime) and isinstance(b, datetime):
        return a.astimezone(timezone.utc) == b.astimezone(timezone.utc)
    return type(a) is type(b) and a == b


def extract_events(text: str, calendar_name: str, win_start: datetime, win_end: datetime, local_tz: tzinfo,
                   default_tz: tzinfo | None = None) -> tuple[list[CalEvent], dict]:
    """Eventos do ICS que tocam [win_start, win_end), já em hora local. `info` descreve limitações encontradas."""
    ctx = _Ctx(local_tz, default_tz)
    raws = parse_events(text, ctx)
    info = {"unsupported_rules": 0, "bad_timezones": 0}
    overrides: dict[tuple[str, str], _Raw] = {}
    for r in raws:
        if r.recurrence_id is not None and r.uid:
            overrides[(r.uid, _key(r.recurrence_id))] = r
    out: list[CalEvent] = []
    horizon = win_end.date() + timedelta(days=1)

    def emit(raw: _Raw, start: date | datetime, uid: str) -> None:
        s_local, all_day = _to_local(start, local_tz)
        dur = _base_duration(raw)
        e_local = s_local + dur
        if not (s_local < win_end and (e_local > win_start or (dur == timedelta(0) and s_local >= win_start))):
            return
        out.append(CalEvent(uid or f"sem-id-{norm_key(raw.title)}", clean_text(raw.title, 160) or "(sem título)", s_local, e_local, all_day, (calendar_name,)))

    for raw in raws:
        if raw.cancelled or raw.start is None:
            continue
        uid = raw.uid
        if raw.recurrence_id is not None:  # exceção de uma série: entra como evento próprio
            emit(raw, raw.start, uid)
            continue
        if raw.rrule:
            occs, understood = expand_rule(raw.rrule, raw.start, horizon)
            if not understood:
                info["unsupported_rules"] += 1
            for occ in occs:
                if any(_same_instant(occ, ex) for ex in raw.exdates) or (uid, _key(occ)) in overrides:
                    continue
                emit(raw, occ, uid)
        elif not any(_same_instant(raw.start, ex) for ex in raw.exdates):
            emit(raw, raw.start, uid)
    info["bad_timezones"] = ctx.bad_tz
    return out, info


def _key(value: date | datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ") if isinstance(value, datetime) else value.strftime("%Y%m%d")


def _base_duration(raw: _Raw) -> timedelta:
    """Duração do evento ORIGINAL (fim - início); vale para todas as ocorrências de uma série."""
    s, e = raw.start, raw.end
    if e is not None and s is not None and type(e) is type(s):
        d = (e - s) if not isinstance(s, datetime) else e.astimezone(timezone.utc) - s.astimezone(timezone.utc)  # type: ignore[operator]
        if d >= timedelta(0):
            return d
    if raw.duration is not None and raw.duration >= timedelta(0):
        return raw.duration
    return timedelta(0) if isinstance(s, datetime) else timedelta(days=1)


def merge_events(groups: list[list[CalEvent]]) -> list[CalEvent]:
    """Junta os calendários, remove duplicatas (mesmo UID+início, ou mesmo título+horário) e ordena por data e hora."""
    seen: dict[tuple, int] = {}
    merged: list[CalEvent] = []
    for events in groups:
        for ev in events:
            keys = [("uid", ev.uid, ev.start.isoformat()), ("sig", norm_key(ev.title), ev.start.isoformat(), ev.end.isoformat(), ev.all_day)]
            hit = next((seen[k] for k in keys if k in seen), None)
            if hit is not None:
                known = merged[hit]
                names = known.calendars + tuple(n for n in ev.calendars if n not in known.calendars)
                merged[hit] = CalEvent(known.uid, known.title, known.start, known.end, known.all_day, names)
                continue
            for k in keys:
                seen[k] = len(merged)
            merged.append(ev)
    return sorted(merged, key=lambda e: (e.start, not e.all_day, e.title.lower()))


# --- serviço --------------------------------------------------------------------------------------------------------------
def default_local_tz() -> tzinfo:
    return datetime.now().astimezone().tzinfo or timezone.utc


class CalendarService:
    NAME = "calendar"

    def __init__(self, cfg: Callable[[], IntegrationsConfig], secrets: SecretStore, cache: IntegrationCache, fetch: Callable[[str], bytes] | None = None,
                 clock: Callable[[], datetime] | None = None, local_tz: tzinfo | None = None) -> None:
        self._cfg, self.secrets, self.cache = cfg, secrets, cache
        self._fetch = fetch or (lambda url: with_retries(lambda: http_get(url, timeout=12, max_bytes=MAX_ICS_BYTES, allowed_hosts=ALLOWED_HOSTS)))
        self.local_tz = local_tz or default_local_tz()
        self._clock = clock or (lambda: datetime.now(self.local_tz))
        self._lock = threading.Lock()
        self._updating = False
        self._notes: list[str] = []
        self._last_errors: dict[str, str] = {}

    @staticmethod
    def secret_name(slot_id: str) -> str:
        return f"calendar:{slot_id}"

    def slots(self):
        return [c for c in self._cfg().calendars if self.secrets.get(self.secret_name(c.id))]

    def configured(self) -> bool:
        return bool(self.slots())

    def busy(self) -> bool:
        """Há uma atualização em andamento NESTE instante (diferente do estado 'aguardando a primeira atualização')."""
        return self._lock.locked()

    def refresh_interval_s(self) -> float:
        return self._cfg().calendar_refresh_min * 60

    # -- atualização --
    def refresh(self) -> ServiceStatus:
        if not self._lock.acquire(blocking=False):
            return self.status()  # já está atualizando: não cria requisição concorrente
        try:
            self._updating, self._last_errors = True, {}
            now = self._clock().astimezone(self.local_tz)
            start = datetime.combine(now.date(), dtime.min, tzinfo=self.local_tz)
            end = start + timedelta(days=self._cfg().calendar_days + 1)
            for slot in self._cfg().calendars:
                url = self.secrets.get(self.secret_name(slot.id))
                if not url:
                    self.cache.delete(self.NAME, f"cal:{slot.id}")
                    continue
                try:
                    text = self._download(url)
                    events, info = extract_events(text, slot.name, start, end, self.local_tz, None)
                    self.cache.put(self.NAME, f"cal:{slot.id}", {"name": slot.name, "events": [e.to_dict() for e in merge_events([events])],
                                                                "info": info, "window": [start.isoformat(), end.isoformat()]})
                except IntegrationError as exc:  # esta agenda falhou; as outras e o cache dela continuam
                    self._last_errors[slot.id] = exc.kind
                    self._record_failure(slot.name, exc)
            if self._last_errors:
                kind = "auth" if "auth" in self._last_errors.values() else next(iter(self._last_errors.values()))
                self.cache.record_failure(self.NAME, kind)
            else:
                self.cache.record_success(self.NAME)
            self._updating = False  # o estado devolvido já não é 'atualizando'
            return self.status()
        finally:
            self._updating = False
            self._lock.release()

    def _download(self, url: str) -> str:
        try:
            raw = self._fetch(url)
        except IntegrationError as exc:
            if exc.kind == "http" and exc.status in (401, 403, 404, 410):
                raise IntegrationError("auth", "O Google não aceitou o endereço do calendário (ele pode ter sido redefinido ou estar incompleto).") from None
            raise
        text = raw.decode("utf-8", errors="replace")
        if "BEGIN:VCALENDAR" not in text[:4000].upper():
            raise IntegrationError("data", "A resposta não é um calendário ICS válido (confira o endereço iCal secreto).")
        return text

    def _record_failure(self, name: str, exc: IntegrationError) -> None:
        log.warning("[CAL] Falha ao atualizar o calendário '%s': %s (%s)", name, exc.kind, exc.message)

    # -- consulta (só lê o cache/memória) --
    def _loaded(self) -> list[tuple[str, dict, datetime]]:
        out = []
        for slot in self._cfg().calendars:
            entry = self.cache.get(self.NAME, f"cal:{slot.id}")
            if entry:
                out.append((slot.id, entry.payload, entry.fetched_at))
        return out

    def events(self, start: datetime, end: datetime) -> list[CalEvent]:
        groups = []
        for _id, payload, _t in self._loaded():
            evs = []
            for d in payload.get("events", []):
                try:
                    evs.append(CalEvent.from_dict(d))
                except (KeyError, ValueError, TypeError):
                    continue
            groups.append(evs)
        return [e for e in merge_events(groups) if e.start < end and (e.end > start or e.start >= start)]

    def notes(self) -> list[str]:
        loaded = self._loaded()
        notes = []
        if any(p.get("info", {}).get("unsupported_rules") for _i, p, _t in loaded):
            notes.append("alguns eventos recorrentes têm regras que não consigo interpretar e podem estar incompletos")
        if any(p.get("info", {}).get("bad_timezones") for _i, p, _t in loaded):
            notes.append("há eventos com fuso horário desconhecido; usei o fuso local")
        return notes

    def last_ok(self) -> datetime | None:
        times = [t for _i, _p, t in self._loaded()]
        return min(times).astimezone(self.local_tz) if times else None

    def status(self) -> ServiceStatus:
        if not self._cfg().calendars or not self.configured():
            return ServiceStatus(self.NAME, ServiceState.UNCONFIGURED, "Cole o endereço iCal secreto em Configurações.")
        if self._updating:
            return ServiceStatus(self.NAME, ServiceState.UPDATING, "Atualizando…", self.last_ok())
        loaded = self._loaded()
        last_ok = self.last_ok()
        if self._last_errors:
            kind = "auth" if "auth" in self._last_errors.values() else next(iter(self._last_errors.values()))
            msg = {"auth": "O Google recusou o endereço do calendário.", "network": "Sem conexão com o Google Calendar.", "timeout": "O Google Calendar demorou demais."}.get(kind, "Falha ao atualizar a agenda.")
            return ServiceStatus(self.NAME, ServiceState.CACHED if loaded else ServiceState.ERROR, msg + (" Mostrando dados salvos." if loaded else ""), last_ok, kind)
        if not loaded:
            st = self.cache.state(self.NAME)
            if st and st.error_kind:
                return ServiceStatus(self.NAME, ServiceState.ERROR, "Ainda não consegui carregar a agenda.", None, st.error_kind)
            return ServiceStatus(self.NAME, ServiceState.UPDATING, "Aguardando a primeira atualização.", None)
        age_s = (self._clock().astimezone(timezone.utc) - min(t for _i, _p, t in loaded)).total_seconds()
        if age_s > self.refresh_interval_s() * 2.5:
            return ServiceStatus(self.NAME, ServiceState.CACHED, "Dados desatualizados (a última atualização foi há mais tempo que o normal).", last_ok)
        return ServiceStatus(self.NAME, ServiceState.OK, "Atualizado.", last_ok)

    def due(self) -> bool:
        if not self.configured():
            return False
        loaded = self._loaded()
        if len(loaded) < len(self.slots()):
            return True
        age = (self._clock().astimezone(timezone.utc) - min(t for _i, _p, t in loaded)).total_seconds()
        return age >= self.refresh_interval_s()

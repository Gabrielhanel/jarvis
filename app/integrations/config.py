"""Configuração NÃO sensível das integrações (vai em data/settings.json, chave "integrations").
Credenciais (endereços iCal, senha de aplicativo) NUNCA passam por aqui: ficam no cofre (secrets.py)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_CALENDARS = 2  # limite da interface inicial; o serviço aceita qualquer quantidade de calendários configurados
MAX_TOPICS = 6
_ID = re.compile(r"[a-z0-9_-]{1,20}")


@dataclass(frozen=True)
class CalendarSlot:
    id: str  # estável; é o nome da credencial no cofre ("calendar:<id>")
    name: str  # nome amigável mostrado ao usuário


@dataclass(frozen=True)
class IntegrationsConfig:
    calendars: tuple[CalendarSlot, ...] = ()
    calendar_refresh_min: int = 15
    calendar_days: int = 7  # hoje + próximos 7 dias
    email_address: str = ""
    email_refresh_min: int = 15
    email_limit: int = 20
    email_ai_classify: bool = True
    news_topics: tuple[str, ...] = ("tecnologia", "economia")
    news_refresh_min: int = 30
    news_per_topic: int = 5
    weather_city: str = ""
    weather_country: str = ""  # código ISO de 2 letras (ex.: BR); opcional, ajuda a escolher a cidade certa
    weather_refresh_min: int = 30
    morning_auto: bool = False  # só liga por escolha explícita do usuário
    morning_speak: bool = True
    morning_ai_polish: bool = False  # 1 chamada à IA para reescrever o resumo; desligado por padrão (o resumo local basta)

    def to_dict(self) -> dict:
        return {
            "calendars": [{"id": c.id, "name": c.name} for c in self.calendars], "calendar_refresh_min": self.calendar_refresh_min,
            "calendar_days": self.calendar_days,
            "email": {"address": self.email_address, "refresh_min": self.email_refresh_min, "limit": self.email_limit, "ai_classify": self.email_ai_classify},
            "news": {"topics": list(self.news_topics), "refresh_min": self.news_refresh_min, "per_topic": self.news_per_topic},
            "weather": {"city": self.weather_city, "country": self.weather_country, "refresh_min": self.weather_refresh_min},
            "morning": {"auto": self.morning_auto, "speak": self.morning_speak, "ai_polish": self.morning_ai_polish},
        }


def _int(value, default: int, low: int, high: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and low <= value <= high else default


def _str(value, limit: int) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def parse_integrations(raw: object) -> IntegrationsConfig:
    """Lê o dicionário salvo com tolerância: valor inválido volta ao padrão (nunca derruba o aplicativo)."""
    d = IntegrationsConfig()
    if not isinstance(raw, dict):
        return d
    cals, seen = [], set()
    for item in raw.get("calendars", []) if isinstance(raw.get("calendars"), list) else []:
        if isinstance(item, dict) and isinstance(item.get("id"), str) and _ID.fullmatch(item["id"]) and item["id"] not in seen:
            seen.add(item["id"])
            cals.append(CalendarSlot(item["id"], _str(item.get("name"), 40) or item["id"]))
    email, news, weather, morning = (raw.get(k) if isinstance(raw.get(k), dict) else {} for k in ("email", "news", "weather", "morning"))
    topics = tuple(t for t in (_str(x, 40) for x in news.get("topics", d.news_topics)) if t)[:MAX_TOPICS] if isinstance(news.get("topics", []), list) else d.news_topics
    country = _str(weather.get("country"), 10).upper()
    country = country if len(country) == 2 and country.isalpha() else ""  # valida ANTES de aceitar: "br1" não vira "BR"
    return IntegrationsConfig(
        calendars=tuple(cals), calendar_refresh_min=_int(raw.get("calendar_refresh_min"), d.calendar_refresh_min, 5, 240),
        calendar_days=_int(raw.get("calendar_days"), d.calendar_days, 1, 31),
        email_address=_str(email.get("address"), 120), email_refresh_min=_int(email.get("refresh_min"), d.email_refresh_min, 5, 240),
        email_limit=_int(email.get("limit"), d.email_limit, 1, 50), email_ai_classify=bool(email.get("ai_classify", d.email_ai_classify)),
        news_topics=topics or d.news_topics, news_refresh_min=_int(news.get("refresh_min"), d.news_refresh_min, 5, 480),
        news_per_topic=_int(news.get("per_topic"), d.news_per_topic, 1, 10),
        weather_city=_str(weather.get("city"), 80), weather_country=country,
        weather_refresh_min=_int(weather.get("refresh_min"), d.weather_refresh_min, 10, 480),
        morning_auto=bool(morning.get("auto", False)), morning_speak=bool(morning.get("speak", True)),
        morning_ai_polish=bool(morning.get("ai_polish", False)),
    )


def ai_allowed_by_mode(mode: object) -> bool:
    """Tarefas extras de IA nas integrações seguem o perfil do modo (ver app/core/modes.py). Valor desconhecido = padrão."""
    from app.core.modes import ai_allowed_by_mode as _allowed
    return _allowed(mode)

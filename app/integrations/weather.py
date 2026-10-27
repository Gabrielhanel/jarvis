"""Clima com Open-Meteo (gratuito, sem chave; uso não comercial; https://open-meteo.com).
A cidade vem da configuração (nada de localização presumida). A resposta mostra a localidade ENCONTRADA para o usuário conferir.
Campos ausentes viram "não informado": o programa nunca afirma chuva (ou ausência de chuva) sem o dado necessário."""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Callable
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from app.integrations.cache import IntegrationCache
from app.integrations.common import IntegrationError, ServiceStatus, clean_text, compute_status, http_get, norm_key, with_retries
from app.integrations.config import IntegrationsConfig

log = logging.getLogger("jarvis.integrations")
ALLOWED_HOSTS = frozenset({"api.open-meteo.com", "geocoding-api.open-meteo.com"})
WMO = {0: "céu limpo", 1: "predominantemente limpo", 2: "parcialmente nublado", 3: "nublado", 45: "neblina", 48: "neblina com geada",
       51: "garoa fraca", 53: "garoa moderada", 55: "garoa intensa", 56: "garoa congelante fraca", 57: "garoa congelante intensa",
       61: "chuva fraca", 63: "chuva moderada", 65: "chuva forte", 66: "chuva congelante fraca", 67: "chuva congelante forte",
       71: "neve fraca", 73: "neve moderada", 75: "neve forte", 77: "grãos de neve", 80: "pancadas de chuva fracas",
       81: "pancadas de chuva moderadas", 82: "pancadas de chuva fortes", 85: "pancadas de neve fracas", 86: "pancadas de neve fortes",
       95: "trovoada", 96: "trovoada com granizo fraco", 99: "trovoada com granizo forte"}


def describe_code(code) -> str:
    return WMO.get(code, "condição não informada") if isinstance(code, int) and not isinstance(code, bool) else "condição não informada"


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _at(seq, i):
    return seq[i] if isinstance(seq, list) and 0 <= i < len(seq) else None


def geocode_url(city: str, country: str) -> str:
    q = {"name": city, "count": 1, "language": "pt", "format": "json"}
    if country:
        q["countryCode"] = country
    return "https://geocoding-api.open-meteo.com/v1/search?" + urlencode(q)


def forecast_url(lat: float, lon: float) -> str:
    q = {"latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}", "timezone": "auto", "forecast_days": 4,
         "current": "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,weather_code,wind_speed_10m",
         "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum"}
    return "https://api.open-meteo.com/v1/forecast?" + urlencode(q, safe=",")


def parse_geocode(data: dict) -> dict:
    results = data.get("results") if isinstance(data, dict) else None
    if not results or not isinstance(results, list) or not isinstance(results[0], dict):
        raise IntegrationError("location", "Não encontrei essa cidade. Confira o nome (e o país, se quiser) em Configurações.")
    r = results[0]
    lat, lon = _num(r.get("latitude")), _num(r.get("longitude"))
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise IntegrationError("data", "A localização veio sem coordenadas válidas.")
    place = ", ".join(x for x in (clean_text(r.get("name"), 60), clean_text(r.get("admin1"), 60), clean_text(r.get("country"), 60)) if x)
    return {"lat": lat, "lon": lon, "place": place or "localização configurada", "tz": clean_text(r.get("timezone"), 60)}


def parse_forecast(data: dict) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("daily"), dict) and not isinstance(data.get("current"), dict):
        raise IntegrationError("data", "A previsão veio em um formato inesperado.")
    cur = data.get("current") if isinstance(data.get("current"), dict) else None
    daily = data.get("daily") if isinstance(data.get("daily"), dict) else {}
    days = []
    for i, d in enumerate(daily.get("time", []) if isinstance(daily.get("time"), list) else []):
        days.append({"date": str(d), "code": _at(daily.get("weather_code"), i), "tmax": _num(_at(daily.get("temperature_2m_max"), i)),
                     "tmin": _num(_at(daily.get("temperature_2m_min"), i)), "pop": _num(_at(daily.get("precipitation_probability_max"), i)),
                     "psum": _num(_at(daily.get("precipitation_sum"), i))})
    if cur is None and not days:
        raise IntegrationError("data", "A previsão veio sem dados utilizáveis.")
    current = None if cur is None else {"temp": _num(cur.get("temperature_2m")), "feels": _num(cur.get("apparent_temperature")),
                                         "humidity": _num(cur.get("relative_humidity_2m")), "precip": _num(cur.get("precipitation")),
                                         "code": cur.get("weather_code"), "wind": _num(cur.get("wind_speed_10m")), "time": str(cur.get("time") or "")}
    return {"current": current, "daily": days, "tz": clean_text(data.get("timezone"), 60)}


def fmt_t(v: float | None) -> str:
    return f"{round(v)}°C" if v is not None else "não informado"


class WeatherService:
    NAME = "weather"

    def __init__(self, cfg: Callable[[], IntegrationsConfig], cache: IntegrationCache, fetch: Callable[[str], bytes] | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self._cfg, self.cache = cfg, cache
        self._fetch = fetch or (lambda url: with_retries(lambda: http_get(url, timeout=10, max_bytes=500_000, allowed_hosts=ALLOWED_HOSTS)))
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._updating = False
        self._error = ""

    def configured(self) -> bool:
        return bool(self._cfg().weather_city)

    def busy(self) -> bool:
        """Há uma atualização em andamento NESTE instante (diferente do estado 'aguardando a primeira atualização')."""
        return self._lock.locked()

    def refresh_interval_s(self) -> float:
        return self._cfg().weather_refresh_min * 60

    def _json(self, url: str) -> dict:
        import json
        try:
            data = json.loads(self._fetch(url).decode("utf-8", errors="replace"))
        except ValueError:
            raise IntegrationError("data", "O serviço de clima devolveu uma resposta inválida.") from None
        if isinstance(data, dict) and data.get("error"):
            raise IntegrationError("http", "O serviço de clima recusou a consulta (limite de uso ou parâmetros).")
        return data

    def _location(self) -> dict:
        cfg = self._cfg()
        key = f"geo:{norm_key(cfg.weather_city)}:{cfg.weather_country}"
        cached = self.cache.get(self.NAME, key)
        if cached and "lat" in cached.payload:
            return cached.payload  # a cidade não muda: geocodifica uma vez só
        loc = parse_geocode(self._json(geocode_url(cfg.weather_city, cfg.weather_country)))
        self.cache.put(self.NAME, key, loc)
        return loc

    def refresh(self) -> ServiceStatus:
        if not self._lock.acquire(blocking=False):
            return self.status()
        try:
            self._updating, self._error = True, ""
            if not self.configured():
                self._updating = False  # o estado devolvido já não é 'atualizando'
                return self.status()
            try:
                loc = self._location()
                snap = parse_forecast(self._json(forecast_url(loc["lat"], loc["lon"])))
                snap.update(place=loc["place"], tz=snap.get("tz") or loc.get("tz") or "")
                self.cache.put(self.NAME, "forecast", snap)
                self.cache.record_success(self.NAME)
            except IntegrationError as exc:
                self._error = exc.kind
                log.warning("[WEATHER] Falha ao atualizar: %s (%s)", exc.kind, exc.message)
                self.cache.record_failure(self.NAME, exc.kind)
            self._updating = False  # o estado devolvido já não é 'atualizando'
            return self.status()
        finally:
            self._updating = False
            self._lock.release()

    def snapshot(self) -> tuple[dict, datetime] | None:
        e = self.cache.get(self.NAME, "forecast")
        return (e.payload, e.fetched_at) if e else None

    def last_ok(self) -> datetime | None:
        s = self.snapshot()
        return s[1].astimezone() if s else None

    def day(self, snap: dict, offset: int) -> tuple[dict | None, bool]:
        """(dia pedido, dados_antigos). 'hoje' é a data de hoje NA LOCALIDADE; se o cache não a contém, os dados estão velhos demais."""
        try:
            today = self._clock().astimezone(ZoneInfo(snap["tz"]) if snap.get("tz") else None).date()
        except Exception:  # noqa: BLE001 - fuso desconhecido no sistema: usa a hora local
            today = self._clock().astimezone().date()
        wanted = date.fromordinal(today.toordinal() + offset).isoformat()
        for d in snap.get("daily", []):
            if d.get("date") == wanted:
                return d, False
        return None, True

    def status(self) -> ServiceStatus:
        s = self.snapshot()
        st = self.cache.state(self.NAME)
        kind = self._error or (st.error_kind if st and st.error_kind else "")
        age = (self._clock().astimezone(timezone.utc) - s[1]).total_seconds() if s else None
        return compute_status(self.NAME, configured=self.configured(), updating=self._updating, last_ok=self.last_ok(), has_data=s is not None,
                              error_kind=kind, age_s=age, interval_s=self.refresh_interval_s(), unconfigured_msg="Informe a cidade em Configurações.",
                              fail_msgs={"location": "Não encontrei a cidade configurada.", "http": "O serviço de clima recusou a consulta."})

    def due(self) -> bool:
        s = self.snapshot()
        return self.configured() and (s is None or (self._clock().astimezone(timezone.utc) - s[1]).total_seconds() >= self.refresh_interval_s())

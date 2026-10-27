"""Radar de notícias: feeds RSS públicos do Google News (pt-BR), sem API paga.
- Só título, fonte, data e link: nada de texto de artigo, nada inventado. O RSS não traz o texto das matérias, então "resumir" é
  listar as manchetes; o link do Google News redireciona para a fonte original e só abre por ação do usuário.
- O XML vem da rede: documentos com DOCTYPE/ENTITY são recusados, o tamanho é limitado e todo texto é higienizado."""
from __future__ import annotations

import logging
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable
from urllib.parse import quote

from app.core.actions import validate_url
from app.integrations.cache import IntegrationCache
from app.integrations.common import IntegrationError, ServiceStatus, clean_text, compute_status, http_get, norm_key, with_retries
from app.integrations.config import IntegrationsConfig

log = logging.getLogger("jarvis.integrations")
ALLOWED_HOSTS = frozenset({"news.google.com"})
_SECTIONS = {"tecnologia": "TECHNOLOGY", "economia": "BUSINESS", "negocios": "BUSINESS", "esportes": "SPORTS", "saude": "HEALTH",
             "ciencia": "SCIENCE", "entretenimento": "ENTERTAINMENT", "mundo": "WORLD", "brasil": "NATION"}
_LOCALE = "hl=pt-BR&gl=BR&ceid=BR:pt-419"


def feed_url(topic: str) -> str:
    section = _SECTIONS.get(norm_key(topic))
    if section:
        return f"https://news.google.com/rss/headlines/section/topic/{section}?{_LOCALE}"
    return f"https://news.google.com/rss/search?q={quote(topic.strip(), safe='')}&{_LOCALE}"


@dataclass(frozen=True)
class NewsItem:
    title: str
    source: str
    published: str | None  # ISO 8601 (UTC) ou None quando o feed não informa
    link: str  # validado: só http/https
    topic: str = ""

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}

    @staticmethod
    def from_dict(d: dict) -> "NewsItem":
        return NewsItem(**{k: d.get(k) or ("" if k != "published" else None) for k in NewsItem.__dataclass_fields__})


def parse_rss(data: bytes, topic: str = "", limit: int = 30) -> list[NewsItem]:
    head = data[:4096].lower()
    if b"<!doctype" in data[:200000].lower() or b"<!entity" in data[:200000].lower() or not (b"<rss" in head or b"<feed" in head or b"<?xml" in head):
        raise IntegrationError("data", "O feed de notícias veio em um formato inesperado; ignorei.")
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise IntegrationError("data", "O feed de notícias está inválido; ignorei.") from None
    items: list[NewsItem] = []
    for node in root.iter("item"):
        title = clean_text(node.findtext("title"), 220)
        link, why = validate_url((node.findtext("link") or "").strip())
        if not title or why:
            continue
        src_node = node.find("source")
        source = clean_text(src_node.text if src_node is not None else "", 60)
        if not source and " - " in title:
            title, _, source = title.rpartition(" - ")
            source = clean_text(source, 60)
        elif source and title.endswith(" - " + source):
            title = title[: -len(source) - 3].rstrip()
        published = None
        try:
            dt = parsedate_to_datetime((node.findtext("pubDate") or "").strip())
            published = (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()
        except (TypeError, ValueError, IndexError):
            pass
        items.append(NewsItem(title, source or "fonte não informada", published, link, topic))
        if len(items) >= limit:
            break
    if not items:
        raise IntegrationError("data", "O feed de notícias não trouxe nenhuma notícia utilizável.")
    return items


def dedupe(items: list[NewsItem], seen: set[str] | None = None) -> list[NewsItem]:
    seen = seen if seen is not None else set()
    out = []
    for it in items:
        key = norm_key(it.title)
        if key and key not in seen:
            seen.add(key)
            out.append(it)
    return out


class NewsService:
    NAME = "news"

    def __init__(self, cfg: Callable[[], IntegrationsConfig], cache: IntegrationCache, fetch: Callable[[str], bytes] | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self._cfg, self.cache = cfg, cache
        self._fetch = fetch or (lambda url: with_retries(lambda: http_get(url, timeout=10, max_bytes=2_000_000, allowed_hosts=ALLOWED_HOSTS)))
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._updating = False
        self._errors: dict[str, str] = {}

    def topics(self) -> tuple[str, ...]:
        return self._cfg().news_topics

    def configured(self) -> bool:
        return bool(self.topics())

    def busy(self) -> bool:
        """Há uma atualização em andamento NESTE instante (diferente do estado 'aguardando a primeira atualização')."""
        return self._lock.locked()

    def refresh_interval_s(self) -> float:
        return self._cfg().news_refresh_min * 60

    @staticmethod
    def _key(topic: str) -> str:
        return "topic:" + norm_key(topic)

    def refresh(self) -> ServiceStatus:
        if not self._lock.acquire(blocking=False):
            return self.status()
        try:
            self._updating, self._errors = True, {}
            for topic in self.topics():
                try:
                    items = parse_rss(self._fetch(feed_url(topic)), topic)
                    self.cache.put(self.NAME, self._key(topic), {"topic": topic, "items": [i.to_dict() for i in items]})
                except IntegrationError as exc:  # este assunto falhou: o cache dele (e os outros assuntos) seguem
                    self._errors[topic] = exc.kind
                    log.warning("[NEWS] Falha em um assunto: %s (%s)", exc.kind, exc.message)
            if self._errors:
                self.cache.record_failure(self.NAME, next(iter(self._errors.values())))
            else:
                self.cache.record_success(self.NAME)
            self._updating = False  # o estado devolvido já não é 'atualizando'
            return self.status()
        finally:
            self._updating = False
            self._lock.release()

    def _topic_items(self, topic: str) -> list[NewsItem]:
        entry = self.cache.get(self.NAME, self._key(topic))
        out = []
        for d in (entry.payload.get("items", []) if entry else []):
            try:
                out.append(NewsItem.from_dict(d))
            except (TypeError, KeyError):
                continue
        return out

    def by_topic(self) -> dict[str, list[NewsItem]]:
        """Notícias por assunto, no máximo `per_topic` cada, sem título repetido (nem entre assuntos: o primeiro assunto fica com ele)."""
        seen: set[str] = set()
        return {t: dedupe(self._topic_items(t), seen)[: self._cfg().news_per_topic] for t in self.topics()}

    def find_topic(self, text: str) -> str | None:
        wanted = norm_key(text)
        return next((t for t in self.topics() if norm_key(t) == wanted), None)

    def headlines(self, n: int = 3) -> list[NewsItem]:
        """As principais manchetes, alternando entre os assuntos (1ª de cada, depois a 2ª de cada...)."""
        groups = [v for v in self.by_topic().values() if v]
        out: list[NewsItem] = []
        for i in range(max((len(g) for g in groups), default=0)):
            out += [g[i] for g in groups if i < len(g)]
        return out[:n]

    def last_ok(self) -> datetime | None:
        times = [e.fetched_at for t in self.topics() if (e := self.cache.get(self.NAME, self._key(t)))]
        return min(times).astimezone() if times else None

    def status(self) -> ServiceStatus:
        times = [e.fetched_at for t in self.topics() if (e := self.cache.get(self.NAME, self._key(t)))]
        st = self.cache.state(self.NAME)
        kind = next(iter(self._errors.values()), "") or (st.error_kind if st and st.error_kind else "")
        age = (self._clock().astimezone(timezone.utc) - min(times)).total_seconds() if times else None
        return compute_status(self.NAME, configured=self.configured(), updating=self._updating, last_ok=self.last_ok(), has_data=bool(times),
                              error_kind=kind, age_s=age, interval_s=self.refresh_interval_s(), unconfigured_msg="Defina os assuntos em Configurações.",
                              fail_msgs={"data": "O feed veio inválido."})

    def due(self) -> bool:
        times = [e.fetched_at for t in self.topics() if (e := self.cache.get(self.NAME, self._key(t)))]
        if len(times) < len(self.topics()):
            return True
        return (self._clock().astimezone(timezone.utc) - min(times)).total_seconds() >= self.refresh_interval_s()

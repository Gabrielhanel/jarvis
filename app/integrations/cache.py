"""Cache persistente das integrações (SQLite). Um registro por (serviço, chave), sempre sobrescrito: o tamanho é limitado por
construção. Cada serviço tem o seu: a falha ou expiração de um não invalida os outros. Falha de banco nunca derruba a integração."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from app.database.database import Database, DatabaseError

log = logging.getLogger("jarvis.integrations")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class CacheEntry:
    payload: dict
    fetched_at: datetime  # UTC


@dataclass(frozen=True)
class StateRow:
    last_ok_at: datetime | None
    last_error_at: datetime | None
    error_kind: str
    failures: int


def _dt(text: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(text) if text else None
    except ValueError:
        return None


class IntegrationCache:
    def __init__(self, db: Database | None, clock=None) -> None:
        self.db = db
        self.clock = clock or utcnow  # o MESMO relógio dos serviços (injetável nos testes)

    def get(self, service: str, key: str = "default") -> CacheEntry | None:
        if self.db is None:
            return None
        try:
            with self.db.connect() as c:
                row = c.execute("SELECT payload, fetched_at FROM integration_cache WHERE service = ? AND key = ?", (service, key)).fetchone()
        except DatabaseError as exc:
            log.warning("[CACHE] leitura falhou para %s: %s", service, exc)
            return None
        if row is None:
            return None
        try:
            payload, fetched = json.loads(row["payload"]), _dt(row["fetched_at"])
        except ValueError:
            return None  # registro corrompido: tratado como ausente (será refeito na próxima atualização)
        return CacheEntry(payload, fetched) if isinstance(payload, dict) and fetched else None

    def put(self, service: str, key: str, payload: dict, fetched_at: datetime | None = None) -> bool:
        if self.db is None:
            return False
        try:
            with self.db.transaction() as c:
                c.execute("INSERT INTO integration_cache (service, key, payload, fetched_at) VALUES (?, ?, ?, ?) "
                          "ON CONFLICT(service, key) DO UPDATE SET payload = excluded.payload, fetched_at = excluded.fetched_at",
                          (service, key, json.dumps(payload, ensure_ascii=False), (fetched_at or self.clock()).isoformat()))
            return True
        except DatabaseError as exc:
            log.warning("[CACHE] gravação falhou para %s: %s", service, exc)
            return False

    def delete(self, service: str, key: str | None = None) -> None:
        if self.db is None:
            return
        try:
            with self.db.transaction() as c:
                if key is None:
                    c.execute("DELETE FROM integration_cache WHERE service = ?", (service,))
                else:
                    c.execute("DELETE FROM integration_cache WHERE service = ? AND key = ?", (service, key))
        except DatabaseError as exc:
            log.warning("[CACHE] remoção falhou para %s: %s", service, exc)

    def keys(self, service: str) -> list[str]:
        if self.db is None:
            return []
        try:
            with self.db.connect() as c:
                return [r["key"] for r in c.execute("SELECT key FROM integration_cache WHERE service = ?", (service,))]
        except DatabaseError:
            return []

    # --- estado de sincronização (metadados; nunca guarda detalhe de erro com dados privados) ---
    def state(self, service: str) -> StateRow | None:
        if self.db is None:
            return None
        try:
            with self.db.connect() as c:
                r = c.execute("SELECT * FROM integration_state WHERE service = ?", (service,)).fetchone()
        except DatabaseError:
            return None
        return StateRow(_dt(r["last_ok_at"]), _dt(r["last_error_at"]), r["error_kind"] or "", r["failures"]) if r else None

    def record_success(self, service: str, when: datetime | None = None) -> None:
        self._write_state(service, "INSERT INTO integration_state (service, last_ok_at, failures) VALUES (?, ?, 0) "
                                   "ON CONFLICT(service) DO UPDATE SET last_ok_at = excluded.last_ok_at, error_kind = NULL, failures = 0",
                          (service, (when or self.clock()).isoformat()))

    def record_failure(self, service: str, kind: str, when: datetime | None = None) -> None:
        self._write_state(service, "INSERT INTO integration_state (service, last_error_at, error_kind, failures) VALUES (?, ?, ?, 1) "
                                   "ON CONFLICT(service) DO UPDATE SET last_error_at = excluded.last_error_at, error_kind = excluded.error_kind, "
                                   "failures = failures + 1", (service, (when or self.clock()).isoformat(), kind[:40]))

    def clear_state(self, service: str) -> None:
        self._write_state(service, "DELETE FROM integration_state WHERE service = ?", (service,))

    def _write_state(self, service: str, sql: str, params: tuple) -> None:
        if self.db is None:
            return
        try:
            with self.db.transaction() as c:
                c.execute(sql, params)
        except DatabaseError as exc:
            log.warning("[CACHE] estado não gravado para %s: %s", service, exc)

"""SQLite local (data/jarvis.db): conexões por operação, migrações atômicas e fechamento seguro.

Decisões (Etapa 6):
- Nenhuma conexão é compartilhada entre threads. Cada operação abre a SUA conexão (barato no SQLite local), usa e fecha.
  Assim a thread principal do Qt, a thread da IA e qualquer outra acessam o banco sem `check_same_thread=False`.
- Cada conexão liga `foreign_keys` e `busy_timeout`. O modo WAL é ativado uma vez (leitores não bloqueiam o escritor).
- Escritas usam `transaction()` (BEGIN IMMEDIATE ... COMMIT/ROLLBACK). Nenhuma transação fica aberta durante chamadas de rede.
- Cada migração roda dentro de UMA transação junto com o `PRAGMA user_version`: ou tudo é aplicado, ou nada.
- `close()` recusa novas operações e espera as que estão em andamento."""
from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

log = logging.getLogger("jarvis.db")

APP_ID = 0x4A525653  # "JRVS": identifica o arquivo como banco do Jarvis
BUSY_TIMEOUT_MS = 5000


class DatabaseError(Exception):
    """Qualquer falha do banco, já separada de erros de programação."""


class DatabaseClosed(DatabaseError):
    """Operação pedida com o banco fechado (ou ainda não aberto)."""


class IntegrityViolation(DatabaseError):
    """Restrição violada (chave estrangeira, UNIQUE, CHECK)."""


class MigrationError(DatabaseError):
    """Uma migração falhou; o banco permanece na versão anterior."""


# Cada item vira a versão N+1 do esquema (PRAGMA user_version). NUNCA edite uma migração já publicada: acrescente outra.
# Não inclua BEGIN/COMMIT: o mecanismo envolve cada item numa transação junto com o registro da versão.
MIGRATIONS: list[str] = [
    # v1 — Etapa 6: conversas, mensagens e memória permanente
    """
    CREATE TABLE conversations (
        id          TEXT PRIMARY KEY,
        title       TEXT NOT NULL CHECK (length(title) <= 200),
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        is_active   INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0, 1))
    );
    CREATE UNIQUE INDEX ux_conversations_active ON conversations(is_active) WHERE is_active = 1;
    CREATE INDEX ix_conversations_updated ON conversations(updated_at);

    CREATE TABLE messages (
        seq            INTEGER PRIMARY KEY AUTOINCREMENT,
        id             TEXT NOT NULL UNIQUE,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        submission_id  TEXT NOT NULL,
        role           TEXT NOT NULL CHECK (role IN ('user', 'jarvis')),
        content        TEXT NOT NULL CHECK (length(content) <= 20000),
        created_at     TEXT NOT NULL,
        source         TEXT,
        origin         TEXT CHECK (origin IS NULL OR origin IN ('local', 'ia')),
        state          TEXT NOT NULL CHECK (state IN
                         ('pending', 'processing', 'done', 'failed', 'unconfirmed', 'interrupted')),
        model          TEXT,
        input_tokens   INTEGER,
        output_tokens  INTEGER,
        duration_s     REAL,
        error_kind     TEXT,
        attempts       INTEGER NOT NULL DEFAULT 1
    );
    CREATE UNIQUE INDEX ux_messages_submission_role ON messages(submission_id, role);
    CREATE INDEX ix_messages_conversation_seq ON messages(conversation_id, seq);
    CREATE INDEX ix_messages_state ON messages(state) WHERE role = 'user' AND state <> 'done';

    CREATE TABLE memories (
        id         TEXT PRIMARY KEY,
        category   TEXT NOT NULL,
        title      TEXT NOT NULL CHECK (length(title) <= 200),
        content    TEXT NOT NULL CHECK (length(content) <= 5000),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        source_conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL
    );
    CREATE INDEX ix_memories_category ON memories(category);
    """,
    # v2 — Etapa 8: cache e estado das integrações; marca respostas PRIVADAS (agenda/e-mail) para não irem à IA como contexto.
    # Só ACRESCENTA: nenhuma tabela ou dado existente é alterado ou apagado. Reversão manual, se um dia for preciso (SQLite >= 3.35):
    #   DROP TABLE integration_cache; DROP TABLE integration_state; ALTER TABLE messages DROP COLUMN private; PRAGMA user_version = 1;
    """
    CREATE TABLE integration_cache (
        service    TEXT NOT NULL CHECK (length(service) <= 40),
        key        TEXT NOT NULL CHECK (length(key) <= 200),
        payload    TEXT NOT NULL CHECK (length(payload) <= 3000000),
        fetched_at TEXT NOT NULL,
        PRIMARY KEY (service, key)
    );
    CREATE TABLE integration_state (
        service       TEXT PRIMARY KEY CHECK (length(service) <= 40),
        last_ok_at    TEXT,
        last_error_at TEXT,
        error_kind    TEXT,
        failures      INTEGER NOT NULL DEFAULT 0
    );
    ALTER TABLE messages ADD COLUMN private INTEGER NOT NULL DEFAULT 0 CHECK (private IN (0, 1));
    """,
    # v3 — Etapa 9: título escolhido pelo usuário, rotinas, execuções de rotinas e eventos de uso (métricas, só METADADOS).
    # Só ACRESCENTA. Os eventos de uso são preenchidos a partir de mensagens JÁ registradas (dados reais; nada é inventado).
    # Reversão manual (SQLite >= 3.35): DROP TABLE usage_events; DROP TABLE routine_runs; DROP TABLE routines;
    #   ALTER TABLE conversations DROP COLUMN title_custom; PRAGMA user_version = 2;
    """
    ALTER TABLE conversations ADD COLUMN title_custom INTEGER NOT NULL DEFAULT 0 CHECK (title_custom IN (0, 1));

    CREATE TABLE routines (
        id             TEXT PRIMARY KEY,
        name           TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 60),
        name_key       TEXT NOT NULL UNIQUE,
        enabled        INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
        trigger_phrase TEXT CHECK (trigger_phrase IS NULL OR length(trigger_phrase) <= 80),
        steps          TEXT NOT NULL CHECK (length(steps) <= 20000),
        created_at     TEXT NOT NULL,
        updated_at     TEXT NOT NULL
    );
    CREATE TABLE routine_runs (
        id           TEXT PRIMARY KEY,
        routine_id   TEXT REFERENCES routines(id) ON DELETE CASCADE,
        routine_name TEXT NOT NULL,
        started_at   TEXT NOT NULL,
        finished_at  TEXT,
        status       TEXT NOT NULL,
        source       TEXT,
        results      TEXT
    );
    CREATE INDEX ix_routine_runs_routine ON routine_runs(routine_id, started_at);

    CREATE TABLE usage_events (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        ts            TEXT NOT NULL,
        submission_id TEXT UNIQUE,
        kind          TEXT NOT NULL CHECK (kind IN ('ai', 'local')),
        category      TEXT,
        source        TEXT,
        model         TEXT,
        input_tokens  INTEGER,
        output_tokens INTEGER,
        duration_s    REAL,
        ok            INTEGER NOT NULL CHECK (ok IN (0, 1)),
        error_kind    TEXT,
        attempts      INTEGER NOT NULL DEFAULT 1
    );
    CREATE INDEX ix_usage_events_ts ON usage_events(ts);

    INSERT INTO usage_events (ts, submission_id, kind, source, model, input_tokens, output_tokens, duration_s, ok, attempts)
        SELECT a.created_at, a.submission_id, CASE a.origin WHEN 'ia' THEN 'ai' ELSE 'local' END, u.source, a.model,
               a.input_tokens, a.output_tokens, a.duration_s, 1, a.attempts
        FROM messages a JOIN messages u ON u.submission_id = a.submission_id AND u.role = 'user'
        WHERE a.role = 'jarvis' AND a.state = 'done';
    INSERT INTO usage_events (ts, submission_id, kind, source, ok, error_kind, attempts)
        SELECT u.created_at, u.submission_id, CASE WHEN u.error_kind = 'memory' THEN 'local' ELSE 'ai' END, u.source, 0, u.error_kind, u.attempts
        FROM messages u
        WHERE u.role = 'user' AND u.state IN ('failed', 'unconfirmed')
          AND NOT EXISTS (SELECT 1 FROM usage_events e WHERE e.submission_id = u.submission_id);
    """,
]


class Database:
    def __init__(self, path: str | Path, migrations: Sequence[str] | None = None) -> None:
        self.path = Path(path)
        self._migrations = list(MIGRATIONS if migrations is None else migrations)
        self._cond = threading.Condition()
        self._active = 0  # operações em andamento (close() espera por elas)
        self._opened = False
        self._closed = False

    # --- ciclo de vida ---
    @property
    def is_open(self) -> bool:
        return self._opened and not self._closed

    def open(self) -> "Database":
        with self._cond:
            if self._opened and not self._closed:
                return self
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = self._connect()
        except (OSError, sqlite3.Error) as exc:
            raise DatabaseError(f"Não consegui abrir o banco {self.path.name}: {exc}") from exc
        try:
            conn.execute(f"PRAGMA application_id = {APP_ID}")
            try:
                conn.execute("PRAGMA journal_mode = WAL")  # opcional: se falhar, segue no modo padrão
            except sqlite3.Error as exc:
                log.warning("[DB] WAL indisponível (%s); seguindo no modo padrão", exc)
            self._migrate(conn)
        except sqlite3.Error as exc:
            raise DatabaseError(f"Falha ao preparar o banco: {exc}") from exc
        finally:
            conn.close()
        with self._cond:
            self._opened, self._closed = True, False
        return self

    def close(self, timeout: float = 5.0) -> None:
        """Recusa novas operações, espera as em andamento e faz o checkpoint do WAL. Pode ser chamado mais de uma vez."""
        with self._cond:
            if not self._opened or self._closed:
                return
            self._closed = True
            if not self._cond.wait_for(lambda: self._active == 0, timeout):
                log.warning("[DB] %d operação(ões) ainda em andamento ao fechar (timeout %.1fs)", self._active, timeout)
        try:
            conn = self._connect()
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                conn.close()
        except sqlite3.Error as exc:
            log.warning("[DB] checkpoint ao fechar falhou: %s", exc)

    def __enter__(self) -> "Database":
        return self.open()

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- conexões e transações ---
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)  # transações explícitas
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        return conn

    @contextmanager
    def _gate(self) -> Iterator[None]:
        with self._cond:
            if not self.is_open:
                raise DatabaseClosed("O banco está fechado.")
            self._active += 1
        try:
            yield
        finally:
            with self._cond:
                self._active -= 1
                self._cond.notify_all()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Conexão própria da chamada (leituras). Fechada ao sair. Erros do SQLite viram DatabaseError."""
        with self._gate():
            conn: sqlite3.Connection | None = None
            try:
                conn = self._connect()
                yield conn
            except sqlite3.IntegrityError as exc:
                raise IntegrityViolation(str(exc)) from exc
            except sqlite3.Error as exc:
                raise DatabaseError(str(exc)) from exc
            finally:
                if conn is not None:
                    conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE ... COMMIT; qualquer exceção desfaz tudo (ROLLBACK)."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                self._rollback(conn)
                raise

    @staticmethod
    def _rollback(conn: sqlite3.Connection) -> None:
        try:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
        except sqlite3.Error as exc:
            log.error("[DB] ROLLBACK falhou: %s", exc)

    @property
    def schema_version(self) -> int:
        with self.connect() as conn:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        """Atalho: uma instrução numa transação própria; devolve as linhas (vazio se não houver)."""
        with self.transaction() as conn:
            cur = conn.execute(sql, params)
            return cur.fetchall() if cur.description else []

    # --- migrações ---
    def _migrate(self, conn: sqlite3.Connection) -> None:
        current = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if current > len(self._migrations):
            raise MigrationError(f"O banco está na versão {current}, mais nova que este aplicativo "
                                 f"({len(self._migrations)}). Atualize o Jarvis; nada foi alterado.")
        for version in range(current + 1, len(self._migrations) + 1):
            script = f"BEGIN IMMEDIATE;\n{self._migrations[version - 1]}\nPRAGMA user_version = {version};\nCOMMIT;"
            try:
                conn.executescript(script)
            except sqlite3.Error as exc:
                self._rollback(conn)  # tabelas e versão voltam ao estado anterior
                raise MigrationError(f"A migração {version} falhou e foi desfeita: {exc}") from exc
            log.info("[DB] Migração %d aplicada", version)

"""Acesso a dados (todo o SQL do Jarvis mora aqui). Consultas SEMPRE parametrizadas; nenhuma conexão é guardada:
cada método usa `Database.connect()` / `transaction()`, então é seguro chamar de qualquer thread."""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from app.database.database import Database, DatabaseError, IntegrityViolation

INCOMPLETE_STATES = ("failed", "unconfirmed", "interrupted", "pending", "processing")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_id() -> str:
    return uuid.uuid4().hex


@dataclass(frozen=True)
class Conversation:
    id: str
    title: str
    created_at: str
    updated_at: str
    is_active: bool
    title_custom: bool = False  # o usuário escolheu o título: nunca é sobrescrito automaticamente


@dataclass(frozen=True)
class ConversationSummary:
    id: str
    title: str
    updated_at: str
    is_active: bool
    message_count: int


@dataclass(frozen=True)
class StoredMessage:
    seq: int
    id: str
    conversation_id: str
    submission_id: str
    role: str  # "user" | "jarvis"
    content: str
    created_at: str
    source: str | None
    origin: str | None  # "local" | "ia" (só nas respostas)
    state: str  # pending | processing | done | failed | unconfirmed | interrupted
    model: str | None
    input_tokens: int | None  # REAIS, devolvidos pela API (None em respostas locais)
    output_tokens: int | None
    duration_s: float | None
    error_kind: str | None
    attempts: int
    private: int = 0  # 1 = resposta com dados privados (agenda/e-mail): nunca volta à IA como contexto


@dataclass(frozen=True)
class Memory:
    id: str
    category: str
    title: str
    content: str
    created_at: str
    updated_at: str
    source_conversation_id: str | None

    @property
    def code(self) -> str:
        return "#" + self.id[:6]


def _conv(r: sqlite3.Row) -> Conversation:
    return Conversation(r["id"], r["title"], r["created_at"], r["updated_at"], bool(r["is_active"]), bool(r["title_custom"]))


def _msg(r: sqlite3.Row) -> StoredMessage:
    return StoredMessage(**{k: r[k] for k in StoredMessage.__dataclass_fields__})


def _mem(r: sqlite3.Row) -> Memory:
    return Memory(**{k: r[k] for k in Memory.__dataclass_fields__})


class ConversationRepository:
    DEFAULT_TITLE = "Nova conversa"

    def __init__(self, db: Database) -> None:
        self.db = db

    # --- conversas ---
    def create(self, title: str | None = None, activate: bool = True) -> str:
        cid, now = new_id(), now_iso()
        with self.db.transaction() as c:
            if activate:
                c.execute("UPDATE conversations SET is_active = 0 WHERE is_active = 1")
            c.execute("INSERT INTO conversations (id, title, created_at, updated_at, is_active) VALUES (?, ?, ?, ?, ?)",
                      (cid, title or self.DEFAULT_TITLE, now, now, 1 if activate else 0))
        return cid

    def get(self, conversation_id: str) -> Conversation | None:
        with self.db.connect() as c:
            row = c.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        return _conv(row) if row else None

    def get_active(self) -> Conversation | None:
        with self.db.connect() as c:
            row = c.execute("SELECT * FROM conversations WHERE is_active = 1").fetchone()
        return _conv(row) if row else None

    def latest(self) -> Conversation | None:
        with self.db.connect() as c:
            row = c.execute("SELECT * FROM conversations ORDER BY updated_at DESC, created_at DESC LIMIT 1").fetchone()
        return _conv(row) if row else None

    def set_active(self, conversation_id: str) -> None:
        with self.db.transaction() as c:
            if c.execute("SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)).fetchone() is None:
                raise DatabaseError(f"Conversa {conversation_id[:8]} não existe.")
            c.execute("UPDATE conversations SET is_active = 0 WHERE is_active = 1 AND id <> ?", (conversation_id,))
            c.execute("UPDATE conversations SET is_active = 1 WHERE id = ?", (conversation_id,))

    def list(self, limit: int = 200) -> list[ConversationSummary]:
        """Conversas da mais recente para a mais antiga, com a quantidade de mensagens (uma consulta só)."""
        with self.db.connect() as c:
            rows = c.execute(
                "SELECT c.id, c.title, c.updated_at, c.is_active, COUNT(m.seq) AS n FROM conversations c "
                "LEFT JOIN messages m ON m.conversation_id = c.id GROUP BY c.id ORDER BY c.updated_at DESC, c.created_at DESC LIMIT ?",
                (max(1, int(limit)),)).fetchall()
        return [ConversationSummary(r["id"], r["title"], r["updated_at"], bool(r["is_active"]), r["n"]) for r in rows]

    def message_count(self, conversation_id: str) -> int:
        with self.db.connect() as c:
            return int(c.execute("SELECT COUNT(*) FROM messages WHERE conversation_id = ?", (conversation_id,)).fetchone()[0])

    def rename(self, conversation_id: str, title: str) -> bool:
        with self.db.transaction() as c:
            return c.execute("UPDATE conversations SET title = ?, title_custom = 1 WHERE id = ?", (title, conversation_id)).rowcount > 0

    def delete(self, conversation_id: str) -> bool:
        """Remove SÓ esta conversa. As mensagens saem por ON DELETE CASCADE; memórias permanentes ficam (a origem vira NULL);
        métricas e rotinas não têm vínculo com a conversa."""
        with self.db.transaction() as c:
            return c.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,)).rowcount > 0

    # --- mensagens ---
    def insert_user(self, conversation_id: str, submission_id: str, content: str, source: str, title: str) -> bool:
        """Registra a mensagem do usuário (estado `pending`). False = esta submissão já estava registrada (nada foi gravado)."""
        now = now_iso()
        with self.db.transaction() as c:
            cur = c.execute(
                "INSERT INTO messages (id, conversation_id, submission_id, role, content, created_at, source, state) "
                "VALUES (?, ?, ?, 'user', ?, ?, ?, 'pending') ON CONFLICT(submission_id, role) DO NOTHING",
                (new_id(), conversation_id, submission_id, content, now, source))
            if cur.rowcount == 0:
                return False
            c.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id))
            c.execute("UPDATE conversations SET title = ? WHERE id = ? AND title = ? AND title_custom = 0",
                      (title, conversation_id, self.DEFAULT_TITLE))  # título local, a partir da 1ª mensagem (nunca sobrescreve o do usuário)
        return True

    def insert_response(self, conversation_id: str, submission_id: str, content: str, origin: str, model: str | None = None,
                        input_tokens: int | None = None, output_tokens: int | None = None, duration_s: float | None = None,
                        attempts: int = 1, private: bool = False) -> bool:
        """Resposta + conclusão da mensagem do usuário + atividade da conversa, numa transação. False = já existia resposta."""
        now = now_iso()
        with self.db.transaction() as c:
            if c.execute("SELECT 1 FROM messages WHERE submission_id = ? AND role = 'user'", (submission_id,)).fetchone() is None:
                raise IntegrityViolation("A mensagem do usuário desta submissão não está registrada (a conversa pode ter sido excluída).")
            cur = c.execute(
                "INSERT INTO messages (id, conversation_id, submission_id, role, content, created_at, origin, state, model, "
                "input_tokens, output_tokens, duration_s, attempts, private) VALUES (?, ?, ?, 'jarvis', ?, ?, ?, 'done', ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(submission_id, role) DO NOTHING",
                (new_id(), conversation_id, submission_id, content, now, origin, model, input_tokens, output_tokens, duration_s, attempts, 1 if private else 0))
            if cur.rowcount == 0:
                return False
            c.execute("UPDATE messages SET state = 'done', error_kind = NULL, attempts = ? "
                      "WHERE submission_id = ? AND role = 'user'", (attempts, submission_id))
            c.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id))
        return True

    def set_state(self, submission_id: str, state: str, error_kind: str | None = None, attempts: int | None = None,
                  only_from: tuple[str, ...] | None = None) -> bool:
        sql, params = "UPDATE messages SET state = ?, error_kind = ?", [state, error_kind]
        if attempts is not None:
            sql, params = sql + ", attempts = ?", params + [attempts]
        sql, params = sql + " WHERE submission_id = ? AND role = 'user' AND state <> 'done'", params + [submission_id]
        if only_from:
            sql += f" AND state IN ({','.join('?' * len(only_from))})"
            params += list(only_from)
        with self.db.transaction() as c:
            return c.execute(sql, params).rowcount > 0

    def mark_interrupted(self) -> int:
        """Ao iniciar: o que ficou `pending`/`processing` de uma execução anterior foi interrompido. NÃO reenvia nada."""
        with self.db.transaction() as c:
            return c.execute("UPDATE messages SET state = 'interrupted' WHERE role = 'user' AND state IN ('pending', 'processing')").rowcount

    def messages(self, conversation_id: str, limit: int | None = None) -> list[StoredMessage]:
        """As últimas `limit` mensagens da conversa, em ordem cronológica (por `seq`, estável)."""
        sql = "SELECT * FROM messages WHERE conversation_id = ? ORDER BY seq DESC"
        params: list = [conversation_id]
        if limit is not None:
            sql, params = sql + " LIMIT ?", params + [max(0, int(limit))]
        with self.db.connect() as c:
            rows = c.execute(sql, params).fetchall()
        return [_msg(r) for r in reversed(rows)]

    def completed_turns(self, conversation_id: str, limit: int) -> list[tuple[str, str]]:
        """Trocas COMPLETAS (pergunta concluída + sua resposta), da mais antiga à mais nova. Falhas/interrompidas ficam de fora."""
        with self.db.connect() as c:
            rows = c.execute(
                "SELECT u.content AS q, a.content AS r FROM messages u "
                "JOIN messages a ON a.submission_id = u.submission_id AND a.role = 'jarvis' "
                "WHERE u.conversation_id = ? AND a.conversation_id = ? AND u.role = 'user' AND u.state = 'done' AND a.private = 0 "
                "ORDER BY u.seq DESC LIMIT ?", (conversation_id, conversation_id, max(0, int(limit)))).fetchall()
        return [(r["q"], r["r"]) for r in reversed(rows)]


class MemoryRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    @property
    def is_open(self) -> bool:
        return self.db.is_open

    def insert(self, category: str, title: str, content: str, conversation_id: str | None) -> Memory:
        mid, now = new_id(), now_iso()
        with self.db.transaction() as c:
            c.execute("INSERT INTO memories (id, category, title, content, created_at, updated_at, source_conversation_id) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?)", (mid, category, title, content, now, now, conversation_id))
        return Memory(mid, category, title, content, now, now, conversation_id)

    def all(self) -> list[Memory]:
        with self.db.connect() as c:
            rows = c.execute("SELECT * FROM memories ORDER BY updated_at DESC, created_at DESC").fetchall()
        return [_mem(r) for r in rows]

    def count(self) -> int:
        with self.db.connect() as c:
            return int(c.execute("SELECT COUNT(*) FROM memories").fetchone()[0])

    def get(self, memory_id: str) -> Memory | None:
        with self.db.connect() as c:
            row = c.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        return _mem(row) if row else None

    def update(self, memory_id: str, category: str, title: str, content: str) -> Memory | None:
        now = now_iso()
        with self.db.transaction() as c:
            if c.execute("UPDATE memories SET category = ?, title = ?, content = ?, updated_at = ? WHERE id = ?",
                         (category, title, content, now, memory_id)).rowcount == 0:
                return None
            row = c.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        return _mem(row)

    def delete(self, memory_id: str) -> bool:
        with self.db.transaction() as c:
            return c.execute("DELETE FROM memories WHERE id = ?", (memory_id,)).rowcount > 0

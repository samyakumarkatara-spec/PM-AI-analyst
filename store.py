"""App state in SQLite: conversations, schema docs, glossary, audit log.

SQLite keeps the MVP zero-ops. Every table carries tenant_id so moving to a
separate Postgres (+ pgvector) later is a mechanical change.
"""
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager

from .config import settings

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT NOT NULL, tenant_id TEXT NOT NULL, messages TEXT NOT NULL, updated_at REAL NOT NULL,
  PRIMARY KEY (tenant_id, id));
CREATE TABLE IF NOT EXISTS schema_docs (
  tenant_id TEXT NOT NULL, table_name TEXT NOT NULL, doc TEXT NOT NULL,
  PRIMARY KEY (tenant_id, table_name));
CREATE TABLE IF NOT EXISTS glossary (
  tenant_id TEXT NOT NULL, term TEXT NOT NULL, definition TEXT NOT NULL, created_at REAL NOT NULL,
  PRIMARY KEY (tenant_id, term));
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, ts REAL NOT NULL,
  conversation_id TEXT, purpose TEXT, sql TEXT, status TEXT, row_count INTEGER,
  elapsed_ms INTEGER, error TEXT);
"""


@contextmanager
def db():
    path = settings.app_db_path
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with _lock:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def init():
    with db() as c:
        c.executescript(SCHEMA)


# ---- conversations ----
def load_messages(conv_id: str) -> list[dict]:
    with db() as c:
        row = c.execute(
            "SELECT messages FROM conversations WHERE tenant_id=? AND id=?",
            (settings.tenant_id, conv_id),
        ).fetchone()
    return json.loads(row["messages"]) if row else []


def save_messages(conv_id: str, messages: list[dict]):
    with db() as c:
        c.execute(
            "INSERT INTO conversations (id, tenant_id, messages, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(tenant_id, id) DO UPDATE SET messages=excluded.messages, updated_at=excluded.updated_at",
            (conv_id, settings.tenant_id, json.dumps(messages), time.time()),
        )


# ---- schema docs ----
def save_schema_docs(docs: dict[str, dict]):
    with db() as c:
        c.execute("DELETE FROM schema_docs WHERE tenant_id=?", (settings.tenant_id,))
        c.executemany(
            "INSERT INTO schema_docs (tenant_id, table_name, doc) VALUES (?,?,?)",
            [(settings.tenant_id, k, json.dumps(v)) for k, v in docs.items()],
        )


def load_schema_docs() -> dict[str, dict]:
    with db() as c:
        rows = c.execute(
            "SELECT table_name, doc FROM schema_docs WHERE tenant_id=?", (settings.tenant_id,)
        ).fetchall()
    return {r["table_name"]: json.loads(r["doc"]) for r in rows}


# ---- glossary (the semantic layer that grows from clarification answers) ----
def glossary_upsert(term: str, definition: str):
    with db() as c:
        c.execute(
            "INSERT INTO glossary (tenant_id, term, definition, created_at) VALUES (?,?,?,?) "
            "ON CONFLICT(tenant_id, term) DO UPDATE SET definition=excluded.definition",
            (settings.tenant_id, term.strip().lower(), definition.strip(), time.time()),
        )


def glossary_all() -> dict[str, str]:
    with db() as c:
        rows = c.execute(
            "SELECT term, definition FROM glossary WHERE tenant_id=? ORDER BY term",
            (settings.tenant_id,),
        ).fetchall()
    return {r["term"]: r["definition"] for r in rows}


def glossary_delete(term: str):
    with db() as c:
        c.execute("DELETE FROM glossary WHERE tenant_id=? AND term=?", (settings.tenant_id, term.lower()))


# ---- audit ----
def audit(conversation_id, purpose, sql, status, row_count=None, elapsed_ms=None, error=None):
    with db() as c:
        c.execute(
            "INSERT INTO audit (tenant_id, ts, conversation_id, purpose, sql, status, row_count, elapsed_ms, error) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (settings.tenant_id, time.time(), conversation_id, purpose, sql, status, row_count, elapsed_ms, error),
        )

"""DB abstraction: SQLite for local dev, Postgres for production.

The wrapper exposes a sqlite3-Connection-compatible API so the rest of the
app doesn't need to know which backend it's talking to.

Set the env var DATABASE_URL to a postgres:// URL to switch to Postgres;
otherwise falls back to a local tasks.db SQLite file.
"""

from __future__ import annotations

import os
import re
import sqlite3
from typing import Any, Iterable

DATABASE_URL = (os.environ.get("DATABASE_URL") or "").strip()
USE_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))

if USE_PG:
    import psycopg2
    import psycopg2.extras

SQLITE_PATH = os.path.join(os.path.dirname(__file__), "tasks.db")


# ── Postgres wrappers (mimic sqlite3 cursor/connection surface) ────
class _PGCursor:
    """Adapter that exposes .fetchone/.fetchall/.lastrowid/.rowcount."""

    def __init__(self, raw, lastrowid=None):
        self._cur = raw
        self.lastrowid = lastrowid
        self.rowcount = raw.rowcount

    def fetchone(self): return self._cur.fetchone()
    def fetchall(self): return self._cur.fetchall()
    def __iter__(self):  return iter(self._cur)


_INSERT_RE = re.compile(r"^\s*INSERT\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)
# Tables without a single auto-id column (composite PK or none).
_NO_ID_TABLES = {"workspace_members"}


class _PGConn:
    """Adapter that takes ?-style SQL and routes to psycopg2."""

    def __init__(self, url: str):
        # SQLAlchemy convention is postgresql:// — psycopg2 accepts both.
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        self._raw = psycopg2.connect(url)
        self.row_factory = None  # for sqlite3 API parity (ignored)

    # Allow ``with conn`` blocks
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self._raw.commit()
        else:
            self._raw.rollback()
        # Don't close — caller may keep the connection (matches sqlite3 ctx manager)

    def commit(self):  self._raw.commit()
    def rollback(self): self._raw.rollback()
    def close(self):   self._raw.close()

    def execute(self, sql: str, params: Iterable[Any] = ()):
        sql_pg = sql.replace("?", "%s")
        m = _INSERT_RE.match(sql_pg)
        is_insert = bool(m)
        table = m.group(1).lower() if m else None
        has_returning = "RETURNING" in sql_pg.upper()
        auto_returning = is_insert and not has_returning and table not in _NO_ID_TABLES
        if auto_returning:
            sql_pg = sql_pg.rstrip(" ;\n\t") + " RETURNING id"

        cur = self._raw.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql_pg, tuple(params))

        lastrowid = None
        if auto_returning and cur.description:
            try:
                row = cur.fetchone()
                if row and "id" in row:
                    lastrowid = row["id"]
            except Exception:
                pass
        return _PGCursor(cur, lastrowid=lastrowid)


def get_db():
    """Return a connection with a sqlite3-Connection-like API."""
    if USE_PG:
        return _PGConn(DATABASE_URL)
    conn = sqlite3.connect(SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def column_exists(conn, table: str, column: str) -> bool:
    if USE_PG:
        rows = conn.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_name = ? AND column_name = ?",
            (table, column),
        ).fetchall()
        return len(rows) > 0
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == column for r in rows)


def auto_id() -> str:
    """SQL fragment for an auto-increment primary key column."""
    return "BIGSERIAL PRIMARY KEY" if USE_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"

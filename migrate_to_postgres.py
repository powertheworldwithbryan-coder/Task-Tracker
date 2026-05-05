"""Port data from local SQLite (tasks.db) to a Postgres DB (e.g. Neon).

Usage (run from the Task Tracker directory):
    set DATABASE_URL=postgres://user:pass@host/db   # PowerShell: $env:DATABASE_URL = "..."
    python migrate_to_postgres.py

The script:
  1. Connects to the local SQLite tasks.db file.
  2. Connects to Postgres via DATABASE_URL.
  3. Ensures the Postgres schema exists (calls app.init_db()).
  4. Copies users, workspaces, workspace_members, tasks, subtasks,
     daily_todos, invites, password_reset_tokens — preserving primary keys.
  5. Resets Postgres sequences so future INSERTs don't collide.

Idempotent-ish: refuses to run if Postgres tables already contain rows.
"""

from __future__ import annotations

import os
import sqlite3
import sys

if not os.environ.get("DATABASE_URL", "").startswith(("postgres://", "postgresql://")):
    sys.exit("ERROR: DATABASE_URL must be set to a postgres:// URL.")

import psycopg2
import psycopg2.extras

# Importing app triggers init_db() against the Postgres DB.
import app  # noqa: F401

PG_URL = os.environ["DATABASE_URL"].replace("postgres://", "postgresql://", 1)
SQLITE_PATH = os.path.join(os.path.dirname(__file__), "tasks.db")

TABLES = [
    "users",
    "workspaces",
    "workspace_members",
    "tasks",
    "subtasks",
    "daily_todos",
    "invites",
    "password_reset_tokens",
]
# Tables whose primary key is a serial id (need sequence reset after copy).
ID_TABLES = [t for t in TABLES if t != "workspace_members"]


def main() -> None:
    src = sqlite3.connect(SQLITE_PATH)
    src.row_factory = sqlite3.Row
    dst = psycopg2.connect(PG_URL)
    dst.autocommit = False

    cur = dst.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # `import app` ran init_db() which bootstrapped a fresh user/workspace in
    # Postgres. Detect that auto-seed and clear it so we can copy the real
    # data over with original PKs. If the DB has more than the auto-seed,
    # bail out — that's user data we shouldn't touch.
    cur.execute("SELECT COUNT(*) AS n FROM users")
    n_users = cur.fetchone()["n"]
    cur.execute("SELECT COUNT(*) AS n FROM tasks")
    n_tasks = cur.fetchone()["n"]
    if n_users > 1 or n_tasks > 0:
        sys.exit(f"ABORT: Postgres has unexpected data ({n_users} users, {n_tasks} tasks). Refusing to clobber.")
    if n_users == 1:
        print("Clearing auto-bootstrap seed row…")
        cur.execute("TRUNCATE users, workspaces, workspace_members, password_reset_tokens RESTART IDENTITY CASCADE")
        dst.commit()

    for t in TABLES:
        rows = src.execute(f"SELECT * FROM {t}").fetchall()
        if not rows:
            print(f"  {t}: 0 rows")
            continue
        cols = list(rows[0].keys())
        placeholders = ", ".join(["%s"] * len(cols))
        col_list = ", ".join(cols)
        sql = f"INSERT INTO {t} ({col_list}) VALUES ({placeholders})"
        for r in rows:
            cur.execute(sql, tuple(r[c] for c in cols))
        print(f"  {t}: {len(rows)} rows")

    # Reset sequences so new inserts don't collide with copied PKs
    for t in ID_TABLES:
        cur.execute(
            f"SELECT setval(pg_get_serial_sequence('{t}', 'id'), "
            f"COALESCE((SELECT MAX(id) FROM {t}), 1), true)"
        )

    dst.commit()
    dst.close()
    src.close()
    print("\nMigration complete.")


if __name__ == "__main__":
    main()

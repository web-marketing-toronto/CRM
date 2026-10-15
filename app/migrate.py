"""Apply SQL migrations in order.

    python -m app.migrate            apply pending migrations
    python -m app.migrate --status   list applied and pending

Each file in migrations/ named NNNN_description.sql runs once, inside its own transaction,
and is recorded in schema_migrations. A Postgres advisory lock serialises concurrent
runners (two Cloud Run instances starting at once), so the second one simply waits and
then finds nothing left to do.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg
import psycopg.sql

from app.config import get_settings

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
LOCK_KEY = 7_283_119_042  # arbitrary, app-wide


def _migration_files() -> list[Path]:
    return sorted(p for p in MIGRATIONS_DIR.glob("*.sql") if p.name[:4].isdigit())


def _ensure_table(conn: psycopg.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            name       text PRIMARY KEY,
            applied_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )


def applied(conn: psycopg.Connection) -> set[str]:
    _ensure_table(conn)
    return {r[0] for r in conn.execute("SELECT name FROM schema_migrations ORDER BY name")}


def migrate(database_url: str | None = None, *, quiet: bool = False) -> list[str]:
    settings = get_settings()
    url = database_url or settings.migrate_database_url or os.environ.get("MIGRATE_DATABASE_URL") or settings.database_url
    ran: list[str] = []
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
        try:
            done = applied(conn)
            for path in _migration_files():
                if path.name in done:
                    continue
                sql = path.read_text(encoding="utf-8")
                with conn.transaction():
                    conn.execute(sql)
                    conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
                ran.append(path.name)
                if not quiet:
                    print(f"applied {path.name}")
            app_password = os.environ.get("CRM_APP_PASSWORD")
            if app_password:
                conn.execute(
                    psycopg.sql.SQL("ALTER ROLE crm_app LOGIN PASSWORD {}").format(psycopg.sql.Literal(app_password))
                )
                if not quiet:
                    print("crm_app login password set")
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
    if not quiet and not ran:
        print("nothing to apply")
    return ran


def status(database_url: str | None = None) -> None:
    settings = get_settings()
    url = database_url or settings.migrate_database_url or settings.database_url
    with psycopg.connect(url, autocommit=True) as conn:
        done = applied(conn)
    for path in _migration_files():
        print(("applied " if path.name in done else "pending ") + path.name)


if __name__ == "__main__":
    if "--status" in sys.argv:
        status()
    else:
        migrate()

"""Database access.

One connection pool, and one way to get a connection: `db(tenant_id, role)`.

Every connection handed out has the row-level-security context set for the duration of
a transaction, so a query cannot accidentally run without a tenant. The role is one of:

    'system'        the app itself (intake, cron worker, auth layer); sees all tenants
    'agency_admin'  a logged-in agency user; sees all tenants
    'owner'/'member' a client user; sees only tenant_id

The transaction commits when the `with` block exits normally and rolls back on error.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.config import get_settings

_pool: ConnectionPool | None = None


def init_pool(database_url: str | None = None) -> ConnectionPool:
    global _pool
    if _pool is not None:
        return _pool
    settings = get_settings()
    _pool = ConnectionPool(
        conninfo=database_url or settings.database_url,
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
        kwargs={"row_factory": dict_row, "autocommit": False},
        open=True,
        name="crm",
    )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def pool() -> ConnectionPool:
    return _pool or init_pool()


@contextmanager
def db(tenant_id: UUID | str | None, role: str) -> Iterator[psycopg.Connection]:
    """A transaction with the RLS context set. Yields a psycopg connection (dict rows)."""
    if role not in ("system", "agency_admin", "owner", "member"):
        raise ValueError(f"unknown role {role!r}")
    with pool().connection() as conn:
        with conn.transaction():
            conn.execute(
                "SELECT set_config('app.tenant_id', %s, true), set_config('app.role', %s, true)",
                (str(tenant_id) if tenant_id else "", role),
            )
            yield conn


@contextmanager
def system_db() -> Iterator[psycopg.Connection]:
    """Privileged transaction for code paths with no user context (intake, worker, auth)."""
    with db(None, "system") as conn:
        yield conn

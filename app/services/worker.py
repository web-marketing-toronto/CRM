"""Background work driven by the minute tick from Cloud Scheduler.

Each tick: send every reminder that has come due, then send any morning digests whose time has
arrived. Both are idempotent (reminders are marked sent inside the same transaction; digests
check the message log), so a double tick or a retried request sends nothing twice.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from psycopg.types.json import Jsonb

from app.db import system_db
from app.services import conversions, followup

log = logging.getLogger("crm.worker")


def run_tick(now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    with system_db() as conn:
        stats = followup.dispatch_due(conn, now=now)
    with system_db() as conn:
        digests = followup.run_digests(conn, now=now)
    with system_db() as conn:
        conv = conversions.process_pending(conn, now=now)
    nightly = None
    try:
        with system_db() as conn:
            nightly = conversions.run_nightly(conn, now=now)
    except Exception:  # noqa: BLE001 - spend is best effort; the next tick after 04:00 tomorrow tries again
        log.exception("nightly spend job failed")
    result = {"at": now.isoformat(), "sent": stats.sent, "cancelled": stats.cancelled, "escalations": stats.escalations, "digests": digests, "conversions": conv, "spend": nightly}
    heartbeat(now, result)
    return result


def heartbeat(now: datetime, result: dict) -> None:
    """Record that the tick ran, so the agency dashboard can show whether the scheduler is alive."""
    try:
        with system_db() as conn:
            conn.execute(
                """
                INSERT INTO job_run (name, ran_on, started_at, finished_at, detail) VALUES ('tick', %s, %s, %s, %s)
                ON CONFLICT (name, ran_on) DO UPDATE SET finished_at = EXCLUDED.finished_at, detail = EXCLUDED.detail
                """,
                (now.date(), now, now, Jsonb({k: v for k, v in result.items() if k != "at"})),
            )
    except Exception:  # noqa: BLE001 - never let bookkeeping break the tick
        log.exception("heartbeat failed")


def dispatch_lead_now(lead_id: UUID) -> None:
    """Send whatever is already due for one lead (used right after a lead arrives or replies, so the
    alert does not wait for the next minute tick)."""
    try:
        with system_db() as conn:
            followup.dispatch_due(conn, lead_id=lead_id)
    except Exception:  # noqa: BLE001 - background task; the minute tick will retry
        log.exception("immediate dispatch failed for lead %s", lead_id)

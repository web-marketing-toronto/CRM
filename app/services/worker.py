"""Background work driven by the minute tick. Phase 2 adds the reminder rules here."""

from __future__ import annotations

from datetime import UTC, datetime

from app.db import system_db


def run_tick() -> dict:
    now = datetime.now(UTC)
    with system_db() as conn:
        due = conn.execute(
            "SELECT count(*) AS n FROM reminder WHERE sent_at IS NULL AND cancelled_at IS NULL AND due_at <= %s", (now,)
        ).fetchone()
    return {"at": now.isoformat(), "reminders_due": int(due["n"]), "sent": 0}

"""The agency's home screen: what needs attention across every client, how the pipes are doing, and
where each client's setup stands. Read-only queries; the router renders them.

Everything runs as 'agency_admin', which the row-level policies treat as privileged, so one query
can span clients.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import psycopg

from app.config import get_settings
from app.services import adplatforms, email_intake

STALE_NEW_HOURS = 24  # a New lead nobody has touched for this long is the agency's problem too
AWAITING_REPLY_HOURS = 2  # a lead who texted back and has waited this long
HEARTBEAT_LATE_MINUTES = 5

OPEN = "l.status NOT IN ('won', 'lost')"
STALE_NEW = "l.status = 'new' AND l.created_at < %(now)s - interval '24 hours'"
AWAITING_REPLY = (
    "l.last_reply_at IS NOT NULL AND COALESCE(l.last_touch_at, '-infinity'::timestamptz) < l.last_reply_at"
)
OVERDUE = "l.next_action_at IS NOT NULL AND l.next_action_at <= %(now)s"


def attention_counts(conn: psycopg.Connection, *, now: datetime) -> dict[str, int]:
    row = conn.execute(
        f"""
        SELECT count(*) FILTER (WHERE {STALE_NEW}) AS stale_new,
               count(*) FILTER (WHERE l.status = 'new' AND l.assigned_to IS NULL) AS unassigned_new,
               count(*) FILTER (WHERE {AWAITING_REPLY} AND l.last_reply_at < %(now)s - interval '2 hours') AS awaiting_reply,
               count(*) FILTER (WHERE {OVERDUE} AND NOT ({STALE_NEW}) AND NOT ({AWAITING_REPLY})) AS overdue,
               count(*) FILTER (WHERE l.flags ? 'needs_review') AS needs_review,
               count(*) AS open_leads
        FROM lead l JOIN tenant t ON t.id = l.tenant_id
        WHERE {OPEN} AND t.is_active
        """,
        {"now": now},
    ).fetchone()
    extra = conn.execute(
        """
        SELECT (SELECT count(*) FROM reminder WHERE rule = 'escalation' AND sent_at > %(now)s - interval '7 days') AS escalations_7d,
               (SELECT count(*) FROM message_log WHERE status = 'failed' AND created_at > %(now)s - interval '24 hours') AS failed_messages_24h,
               (SELECT count(*) FROM conversion_event WHERE status = 'failed') AS conversions_failed,
               (SELECT count(*) FROM conversion_event WHERE status = 'pending') AS conversions_pending
        """,
        {"now": now},
    ).fetchone()
    return {**row, **extra}


WHY_LABELS = {
    "untouched": "New lead, nobody has called",
    "awaiting_reply": "Lead replied, no answer yet",
    "overdue": "Follow-up overdue",
}


def attention_leads(conn: psycopg.Connection, *, now: datetime, limit: int = 30) -> list[dict]:
    """Leads across every active client that somebody at the agency should look at, oldest problem first."""
    rows = conn.execute(
        f"""
        SELECT l.id, l.tenant_id, t.name AS tenant_name, l.ref_code, l.contact_name, l.status, l.job_type, l.channel, l.platform,
               l.created_at, l.last_touch_at, l.last_reply_at, l.next_action_at, u.name AS assignee,
               CASE WHEN {STALE_NEW} THEN 'untouched'
                    WHEN {AWAITING_REPLY} THEN 'awaiting_reply'
                    ELSE 'overdue' END AS why,
               CASE WHEN {STALE_NEW} THEN l.created_at
                    WHEN {AWAITING_REPLY} THEN l.last_reply_at
                    ELSE l.next_action_at END AS since
        FROM lead l
        JOIN tenant t ON t.id = l.tenant_id
        LEFT JOIN app_user u ON u.id = l.assigned_to
        WHERE {OPEN} AND t.is_active AND NOT (l.flags ? 'needs_review')
          AND (({STALE_NEW}) OR ({AWAITING_REPLY} AND l.last_reply_at < %(now)s - interval '2 hours') OR ({OVERDUE}))
        ORDER BY since ASC
        LIMIT %(limit)s
        """,
        {"now": now, "limit": limit},
    ).fetchall()
    for r in rows:
        r["why_label"] = WHY_LABELS[r["why"]]
    return rows


def volume(conn: psycopg.Connection, *, now: datetime, tz_name: str = "America/Toronto") -> dict[str, Any]:
    local = now.astimezone(ZoneInfo(tz_name))
    day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = day_start - timedelta(days=day_start.weekday())
    row = conn.execute(
        """
        SELECT count(*) FILTER (WHERE l.created_at >= %(day)s) AS today,
               count(*) FILTER (WHERE l.created_at >= %(week)s) AS this_week,
               count(*) FILTER (WHERE l.created_at > %(now)s - interval '30 days') AS last_30,
               count(*) FILTER (WHERE l.status = 'won' AND l.won_at > %(now)s - interval '30 days') AS won_30,
               coalesce(sum(l.deal_value) FILTER (WHERE l.status = 'won' AND l.won_at > %(now)s - interval '30 days'), 0) AS won_value_30,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (l.first_contact_at - l.created_at)) / 60)
                   FILTER (WHERE l.created_at > %(now)s - interval '30 days' AND l.first_contact_at IS NOT NULL) AS speed_median_min
        FROM lead l JOIN tenant t ON t.id = l.tenant_id
        WHERE t.is_active
        """,
        {"now": now, "day": day_start, "week": week_start},
    ).fetchone()
    return dict(row)


def client_rows(conn: psycopg.Connection, *, now: datetime) -> list[dict]:
    """One row per client with activity counts and the setup steps still missing."""
    rows = conn.execute(
        """
        SELECT t.*,
               (SELECT count(*) FROM lead l WHERE l.tenant_id = t.id AND l.status NOT IN ('won', 'lost')) AS open_leads,
               (SELECT count(*) FROM lead l WHERE l.tenant_id = t.id AND l.status = 'new') AS new_leads,
               (SELECT count(*) FROM lead l WHERE l.tenant_id = t.id AND l.status = 'new' AND l.created_at < %(now)s - interval '24 hours') AS stale_new,
               (SELECT count(*) FROM lead l WHERE l.tenant_id = t.id AND l.created_at > %(now)s - interval '7 days') AS leads_7d,
               (SELECT count(*) FROM lead l WHERE l.tenant_id = t.id AND l.created_at > %(now)s - interval '30 days') AS leads_30d,
               (SELECT count(*) FROM lead l WHERE l.tenant_id = t.id AND l.status = 'won' AND l.won_at > %(now)s - interval '30 days') AS won_30d,
               (SELECT coalesce(sum(deal_value), 0) FROM lead l WHERE l.tenant_id = t.id AND l.status = 'won' AND l.won_at > %(now)s - interval '30 days') AS won_value_30d,
               (SELECT max(created_at) FROM lead l WHERE l.tenant_id = t.id) AS last_lead_at,
               (SELECT count(*) FROM lead l WHERE l.tenant_id = t.id AND l.channel IN ('website_form', 'call', 'meta_lead_ad', 'google_lead_form')) AS wired_leads,
               (SELECT count(*) FROM app_user u WHERE u.tenant_id = t.id AND u.is_active) AS user_count,
               (SELECT count(*) FROM app_user u WHERE u.tenant_id = t.id AND u.is_active AND u.role = 'owner') AS owner_count,
               (SELECT count(*) FROM app_user u WHERE u.tenant_id = t.id AND u.is_active AND u.last_login_at IS NOT NULL) AS signed_in_users,
               (SELECT count(*) FROM app_user u WHERE u.tenant_id = t.id AND u.is_active AND u.mobile_e164 IS NOT NULL AND u.notify_sms) AS texting_users
        FROM tenant t
        ORDER BY t.is_active DESC, t.name
        """,
        {"now": now},
    ).fetchall()
    for r in rows:
        r["gaps"] = setup_gaps(r)
    return rows


def setup_gaps(t: dict) -> list[str]:
    """What a client still needs before leads flow and alerts reach a phone. Empty means ready."""
    gaps: list[str] = []
    if not t["owner_count"]:
        gaps.append("No owner login")
    elif not t["signed_in_users"]:
        gaps.append("Nobody has signed in yet")
    if not t["texting_users"]:
        gaps.append("No mobile for text alerts")
    if not t.get("twilio_number"):
        gaps.append("No texting number")
    if not (t.get("service_area") or {}):
        gaps.append("No service area")
    if not (t.get("job_types") or []):
        gaps.append("No job types")
    if not t["wired_leads"]:
        gaps.append("No leads from the website yet")
    return gaps


def recent_leads(conn: psycopg.Connection, *, limit: int = 12) -> list[dict]:
    return conn.execute(
        """
        SELECT l.id, l.tenant_id, t.name AS tenant_name, l.ref_code, l.contact_name, l.status, l.channel, l.platform, l.job_type,
               l.created_at, l.first_contact_at, u.name AS assignee
        FROM lead l JOIN tenant t ON t.id = l.tenant_id LEFT JOIN app_user u ON u.id = l.assigned_to
        WHERE t.is_active
        ORDER BY l.created_at DESC
        LIMIT %s
        """,
        (limit,),
    ).fetchall()


def delivery_problems(
    conn: psycopg.Connection, *, now: datetime, hours: int = 24, limit: int = 20
) -> list[dict]:
    return conn.execute(
        """
        SELECT m.id, m.created_at, m.channel, m.kind, m.to_addr, m.error, m.lead_id, m.tenant_id, t.name AS tenant_name, u.name AS user_name
        FROM message_log m LEFT JOIN tenant t ON t.id = m.tenant_id LEFT JOIN app_user u ON u.id = m.user_id
        WHERE m.status = 'failed' AND m.created_at > %s - make_interval(hours => %s)
        ORDER BY m.created_at DESC
        LIMIT %s
        """,
        (now, hours, limit),
    ).fetchall()


def failed_uploads(conn: psycopg.Connection, *, limit: int = 20) -> list[dict]:
    """Conversion uploads that gave up, with the reason, so the fix is obvious from the dashboard."""
    return conn.execute(
        """
        SELECT c.id, c.platform, c.action, c.value, c.last_error, c.attempted_at, c.created_at, c.lead_id, c.tenant_id,
               t.name AS tenant_name, l.ref_code, l.contact_name
        FROM conversion_event c JOIN tenant t ON t.id = c.tenant_id JOIN lead l ON l.id = c.lead_id
        WHERE c.status = 'failed' ORDER BY coalesce(c.attempted_at, c.created_at) DESC LIMIT %s
        """,
        (limit,),
    ).fetchall()


def pipes(conn: psycopg.Connection, *, now: datetime) -> dict[str, Any]:
    """Is each outside connection alive: texting, email, the scheduler tick, ad-platform uploads."""
    s = get_settings()
    m = conn.execute(
        """
        SELECT max(created_at) FILTER (WHERE channel = 'sms' AND direction = 'out' AND status IN ('sent', 'delivered')) AS sms_last_sent,
               max(created_at) FILTER (WHERE channel = 'sms' AND direction = 'in') AS sms_last_received,
               count(*) FILTER (WHERE channel = 'sms' AND status = 'failed' AND created_at > %(now)s - interval '24 hours') AS sms_failed_24h,
               max(created_at) FILTER (WHERE channel = 'email' AND direction = 'out' AND status IN ('sent', 'delivered')) AS email_last_sent,
               count(*) FILTER (WHERE channel = 'email' AND status = 'failed' AND created_at > %(now)s - interval '24 hours') AS email_failed_24h
        FROM message_log
        """,
        {"now": now},
    ).fetchone()
    jobs = conn.execute(
        """
        SELECT (SELECT max(finished_at) FROM job_run WHERE name = 'tick') AS tick_at,
               (SELECT max(finished_at) FROM job_run WHERE name = 'email_intake') AS mail_at,
               (SELECT detail->>'error' FROM job_run WHERE name = 'email_intake' ORDER BY finished_at DESC NULLS LAST LIMIT 1) AS mail_error,
               (SELECT count(*) FROM inbound_email WHERE status IN ('lead', 'duplicate') AND created_at > %(now)s - interval '24 hours') AS mail_leads_24h,
               (SELECT count(*) FROM inbound_email WHERE status = 'rejected' AND created_at > %(now)s - interval '24 hours') AS mail_rejected_24h,
               (SELECT max(finished_at) FROM job_run WHERE name = 'spend') AS spend_at,
               (SELECT count(*) FROM conversion_event WHERE status = 'pending') AS conv_pending,
               (SELECT count(*) FROM conversion_event WHERE status = 'failed') AS conv_failed,
               (SELECT max(sent_at) FROM conversion_event WHERE status = 'sent') AS conv_last_sent,
               (SELECT count(*) FROM ad_creative WHERE status = 'ok') AS ads_ok,
               (SELECT count(*) FROM ad_creative WHERE status = 'error') AS ads_failing,
               (SELECT error FROM ad_creative WHERE status = 'error' ORDER BY fetched_at DESC NULLS LAST LIMIT 1) AS ads_error
        """,
        {"now": now},
    ).fetchone()
    tick_late = jobs["tick_at"] is None or (now - jobs["tick_at"]) > timedelta(minutes=HEARTBEAT_LATE_MINUTES)
    return {
        "sms_provider": s.sms_provider,
        "sms_live": s.sms_provider == "twilio",
        "agency_number": s.twilio_agency_from,
        "email_provider": s.email_provider,
        "email_live": s.email_provider in ("smtp", "postmark", "sendgrid"),
        "email_from": s.email_from,
        "ads_live": s.ads_provider == "live", "ads_provider": s.ads_provider,
        "google_creds": adplatforms.google_creds_source(),
        "mail_enabled": email_intake.enabled(), "mail_user": s.imap_user or s.smtp_user or s.email_from,
        "mail_late": jobs["mail_at"] is None or (now - jobs["mail_at"]) > timedelta(minutes=HEARTBEAT_LATE_MINUTES),
        "meta_configured": bool(s.meta_app_secret and s.meta_access_token),
        "meta_token": bool(s.meta_access_token), "meta_webhook": bool(s.meta_app_secret and s.meta_verify_token),
        **m,
        **jobs,
        "tick_late": tick_late,
    }


def team(conn: psycopg.Connection) -> list[dict]:
    return conn.execute(
        "SELECT * FROM app_user WHERE role = 'agency_admin' ORDER BY is_active DESC, name"
    ).fetchall()


def build(conn: psycopg.Connection, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    return {
        "now": now,
        "attention": attention_counts(conn, now=now),
        "attention_leads": attention_leads(conn, now=now),
        "volume": volume(conn, now=now),
        "clients": client_rows(conn, now=now),
        "recent": recent_leads(conn),
        "problems": delivery_problems(conn, now=now),
        "failed_uploads": failed_uploads(conn),
        "pipes": pipes(conn, now=now),
        "team_count": conn.execute(
            "SELECT count(*) AS n FROM app_user WHERE role = 'agency_admin' AND is_active"
        ).fetchone()["n"],
    }

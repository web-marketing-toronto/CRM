"""The follow-up engine: what to remind whom, when, and what happens when they answer.

Two clocks drive it: time since the lead arrived (while it is New) and time since the last
logged touch (after that). Every event below cancels what no longer applies and schedules what
does; `dispatch_due` sends whatever is due and `run_digests` sends the morning lists. All
times are UTC internally; business hours are evaluated in the tenant's time zone.

Rules (reminder.rule):
    new_alert      the moment a lead arrives
    nudge          still New after N hours (business hours)
    cadence        day 1 / 3 / 7 / 14 after the last touch; the last one asks W / L / K
    reply_nudge    lead replied and nobody responded within N hours
    replied        immediate alert that the lead replied
    resubmit       the same person submitted again
    close_loop     quoted N days ago: did it close?
    snoozed        snooze is over
    escalation     to the agency: untouched for N hours, or reminders ignored
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psycopg
from psycopg.types.json import Jsonb

from app.auth import crypto
from app.config import get_settings
from app.models import PLATFORM_LABELS, ActivityType, Channel, LeadStatus, LostReason, Platform, Role
from app.services import leads as lead_svc
from app.services import messaging
from app.services.normalize import format_phone_display

log = logging.getLogger("crm.followup")

DEFAULTS: dict[str, Any] = {
    "nudge_hours": [1, 4],
    "cadence_days": [1, 3, 7, 14],
    "escalate_after_hours": 24,
    "close_loop_days": [10, 20],
    "reply_nudge_hours": 4,
    "digest_time": "07:30",
    "ignored_before_escalation": 2,
}

CLIENT_RULES = ("new_alert", "nudge", "cadence", "reply_nudge", "replied", "resubmit", "close_loop", "snoozed", "after_appointment")
LEAD_FACING_RULES = ("ack", "decline", "appointment")


def settings_for(tenant: dict) -> dict[str, Any]:
    merged = dict(DEFAULTS)
    merged.update((tenant.get("settings") or {}).get("followup") or {})
    return merged


def tz_for(tenant: dict) -> ZoneInfo:
    try:
        return ZoneInfo(tenant.get("timezone") or "America/Toronto")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("America/Toronto")


def _hm(text: str, default: time) -> time:
    try:
        h, m = text.split(":")
        return time(int(h), int(m))
    except (ValueError, AttributeError):
        return default


def next_business_time(dt: datetime, tenant: dict) -> datetime:
    """`dt` if it falls inside business hours on a working day, else the start of the next window."""
    hours = tenant.get("business_hours") or {}
    tz = tz_for(tenant)
    start = _hm(hours.get("start", "07:00"), time(7, 0))
    end = _hm(hours.get("end", "19:00"), time(19, 0))
    days = {int(d) for d in hours.get("days", [1, 2, 3, 4, 5, 6])} or {1, 2, 3, 4, 5, 6}
    local = dt.astimezone(tz)
    for _ in range(10):
        weekday = local.isoweekday()
        if weekday in days:
            if start <= local.time() < end:
                return local.astimezone(UTC)
            if local.time() < start:
                return local.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0).astimezone(UTC)
        local = (local + timedelta(days=1)).replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
    return dt


# --- Scheduling primitives ------------------------------------------------------------------

def schedule(conn: psycopg.Connection, *, tenant_id: UUID, lead_id: UUID, user_id: UUID | None, rule: str, due_at: datetime, key: str, payload: dict | None = None) -> UUID | None:
    row = conn.execute(
        """
        INSERT INTO reminder (tenant_id, lead_id, user_id, rule, channel, idempotency_key, due_at, payload)
        VALUES (%s, %s, %s, %s, 'auto', %s, %s, %s)
        ON CONFLICT (idempotency_key) DO NOTHING
        RETURNING id
        """,
        (tenant_id, lead_id, user_id, rule, key, due_at, Jsonb(payload or {})),
    ).fetchone()
    return row["id"] if row else None


def cancel(conn: psycopg.Connection, lead_id: UUID, rules: tuple[str, ...] | None = None) -> int:
    if rules:
        cur = conn.execute(
            "UPDATE reminder SET cancelled_at = now() WHERE lead_id = %s AND sent_at IS NULL AND cancelled_at IS NULL AND rule = ANY(%s)",
            (lead_id, list(rules)),
        )
    else:
        cur = conn.execute("UPDATE reminder SET cancelled_at = now() WHERE lead_id = %s AND sent_at IS NULL AND cancelled_at IS NULL", (lead_id,))
    return cur.rowcount


def acknowledge(conn: psycopg.Connection, lead_id: UUID, user_id: UUID | None = None) -> None:
    conn.execute("UPDATE reminder SET acknowledged_at = now() WHERE lead_id = %s AND sent_at IS NOT NULL AND acknowledged_at IS NULL", (lead_id,))


def _ts(dt: datetime) -> str:
    return str(int(dt.timestamp()))


def _skip(lead: dict) -> bool:
    flags = lead.get("flags") or {}
    return lead.get("channel") == Channel.IMPORT.value or bool(flags.get("needs_review"))


# --- Events ---------------------------------------------------------------------------------

def on_lead_created(conn: psycopg.Connection, tenant: dict, lead: dict, *, now: datetime | None = None, alert: bool = True) -> None:
    if _skip(lead):
        return
    now = now or datetime.now(UTC)
    base = lead.get("created_at") or now
    cfg = settings_for(tenant)
    tid, lid, uid = tenant["id"], lead["id"], lead.get("assigned_to")
    cancel(conn, lid)
    if alert:
        schedule(conn, tenant_id=tid, lead_id=lid, user_id=uid, rule="new_alert", due_at=now, key=f"{lid}:new_alert:{_ts(base)}")
        from app.services import lead_messages

        lead_messages.plan_for_new_lead(conn, tenant, lead, now=now)
    for i, hours in enumerate(cfg["nudge_hours"], start=1):
        schedule(conn, tenant_id=tid, lead_id=lid, user_id=uid, rule="nudge", due_at=next_business_time(base + timedelta(hours=float(hours)), tenant), key=f"{lid}:nudge:{i}:{_ts(base)}", payload={"n": i})
    schedule(conn, tenant_id=tid, lead_id=lid, user_id=None, rule="escalation", due_at=base + timedelta(hours=float(cfg["escalate_after_hours"])), key=f"{lid}:escalation:new:{_ts(base)}", payload={"why": "untouched"})


def _schedule_cadence(conn: psycopg.Connection, tenant: dict, lead: dict, base: datetime) -> None:
    cfg = settings_for(tenant)
    tid, lid, uid = tenant["id"], lead["id"], lead.get("assigned_to")
    days = list(cfg["cadence_days"])
    for i, d in enumerate(days, start=1):
        schedule(
            conn, tenant_id=tid, lead_id=lid, user_id=uid, rule="cadence",
            due_at=next_business_time(base + timedelta(days=float(d)), tenant),
            key=f"{lid}:cadence:{i}:{_ts(base)}", payload={"n": i, "final": i == len(days)},
        )
    conn.execute("UPDATE lead SET cadence_base = %s, snoozed_until = NULL WHERE id = %s", (base, lid))


def on_touch(conn: psycopg.Connection, tenant: dict, lead: dict, *, now: datetime | None = None) -> None:
    """A call, text or email was logged (or the status moved to Contacted / No response)."""
    if _skip(lead) or lead.get("status") in (LeadStatus.WON.value, LeadStatus.LOST.value):
        return
    now = now or datetime.now(UTC)
    cancel(conn, lead["id"], ("new_alert", "nudge", "escalation", "cadence", "reply_nudge", "snoozed", "close_loop"))
    acknowledge(conn, lead["id"])
    _schedule_cadence(conn, tenant, lead, now)


def on_status_change(conn: psycopg.Connection, tenant: dict, lead: dict, old: LeadStatus, new: LeadStatus, *, now: datetime | None = None) -> None:
    if _skip(lead):
        return
    now = now or datetime.now(UTC)
    lid = lead["id"]
    acknowledge(conn, lid)
    if new in (LeadStatus.WON, LeadStatus.LOST):
        cancel(conn, lid)
        return
    if new == LeadStatus.QUOTED:
        cancel(conn, lid)
        cfg = settings_for(tenant)
        for i, d in enumerate(cfg["close_loop_days"], start=1):
            schedule(conn, tenant_id=tenant["id"], lead_id=lid, user_id=lead.get("assigned_to"), rule="close_loop", due_at=next_business_time(now + timedelta(days=float(d)), tenant), key=f"{lid}:close_loop:{i}:{_ts(now)}", payload={"n": i})
        return
    if new == LeadStatus.NEW:
        on_lead_created(conn, tenant, {**lead, "created_at": now, "status": "new"}, now=now, alert=old.is_closed)
        return
    if new in (LeadStatus.CONTACTED, LeadStatus.NO_RESPONSE):
        on_touch(conn, tenant, {**lead, "status": new.value}, now=now)


def on_lead_replied(conn: psycopg.Connection, tenant: dict, lead: dict, text: str, *, now: datetime | None = None, alert: bool = True) -> None:
    """The lead texted (or called) back. Alert now, nudge in a few hours if nobody responds, restart the cadence.
    `alert=False` when the client already knows (their phone just rang)."""
    now = now or datetime.now(UTC)
    lid, tid, uid = lead["id"], tenant["id"], lead.get("assigned_to")
    conn.execute("UPDATE lead SET last_reply_at = %s, next_action_at = %s WHERE id = %s", (now, now, lid))
    lead_svc.log_activity(conn, tid, lid, ActivityType.LEAD_REPLIED, actor_label=lead.get("contact_name") or "lead", body=text[:2000], occurred_at=now)
    if lead.get("status") == LeadStatus.NO_RESPONSE.value:
        conn.execute("UPDATE lead SET status = 'contacted' WHERE id = %s", (lid,))
        lead_svc.log_activity(conn, tid, lid, ActivityType.STATUS_CHANGE, detail={"from": "no_response", "to": "contacted", "auto": True, "because": "lead replied"}, occurred_at=now)
    if lead.get("status") in (LeadStatus.WON.value, LeadStatus.LOST.value):
        if alert:
            schedule(conn, tenant_id=tid, lead_id=lid, user_id=uid, rule="replied", due_at=now, key=f"{lid}:replied:{_ts(now)}", payload={"text": text[:300]})
        return
    cancel(conn, lid, ("cadence", "reply_nudge", "nudge", "snoozed"))
    cfg = settings_for(tenant)
    if alert:
        schedule(conn, tenant_id=tid, lead_id=lid, user_id=uid, rule="replied", due_at=now, key=f"{lid}:replied:{_ts(now)}", payload={"text": text[:300]})
        schedule(conn, tenant_id=tid, lead_id=lid, user_id=uid, rule="reply_nudge", due_at=now + timedelta(hours=float(cfg["reply_nudge_hours"])), key=f"{lid}:reply_nudge:{_ts(now)}", payload={"text": text[:300]})
    _schedule_cadence(conn, tenant, lead, now)


def on_resubmitted(conn: psycopg.Connection, tenant: dict, lead: dict, *, now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)
    schedule(conn, tenant_id=tenant["id"], lead_id=lead["id"], user_id=lead.get("assigned_to"), rule="resubmit", due_at=now, key=f"{lead['id']}:resubmit:{_ts(now)}")


def on_snooze(conn: psycopg.Connection, tenant: dict, lead: dict, days: int, *, now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    lid = lead["id"]
    cancel(conn, lid, ("cadence", "reply_nudge", "nudge", "snoozed", "escalation"))  # snoozing is engagement; nothing to escalate
    acknowledge(conn, lid)
    until = next_business_time(now + timedelta(days=days), tenant)
    conn.execute("UPDATE lead SET snoozed_until = %s, next_action_at = %s WHERE id = %s", (until, until, lid))
    schedule(conn, tenant_id=tenant["id"], lead_id=lid, user_id=lead.get("assigned_to"), rule="snoozed", due_at=until, key=f"{lid}:snoozed:{_ts(now)}")
    return until


def on_keep_trying(conn: psycopg.Connection, tenant: dict, lead: dict, *, now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)
    cancel(conn, lead["id"], ("cadence", "reply_nudge", "snoozed"))
    acknowledge(conn, lead["id"])
    _schedule_cadence(conn, tenant, lead, now)


# --- Recipients and message text -------------------------------------------------------------

def _recipients(conn: psycopg.Connection, tenant_id: UUID, assigned_to: UUID | None) -> list[dict]:
    if assigned_to:
        rows = conn.execute("SELECT * FROM app_user WHERE id = %s AND is_active", (assigned_to,)).fetchall()
        if rows:
            return rows
    return conn.execute("SELECT * FROM app_user WHERE tenant_id = %s AND role = 'owner' AND is_active", (tenant_id,)).fetchall()


def _agency_recipients(conn: psycopg.Connection) -> list[dict]:
    return conn.execute("SELECT * FROM app_user WHERE role = 'agency_admin' AND is_active AND (notify_sms OR notify_email)").fetchall()


def _lead_line(lead: dict) -> str:
    name = lead.get("contact_name") or lead.get("company") or format_phone_display(lead.get("phone_e164")) or "Unknown"
    bits = [f"{name} ({lead['ref_code']})"]
    what = lead.get("job_type")
    where = (lead.get("job_address") or "").split(",")
    town = where[1].strip() if len(where) > 1 else (where[0].strip() if where and where[0] else "")
    if what and town:
        bits.append(f"{what} in {town}")
    elif what or town:
        bits.append(what or town)
    return ", ".join(bits)


def _contact_line(lead: dict, tenant: dict | None = None) -> str:
    """How to reach the lead. When the client has a business texting number, point at the app (where calls
    and texts go through that number) rather than printing the raw mobile, which invites a call from a
    personal phone that the lead would then save."""
    if tenant is not None and tenant.get("twilio_number"):
        return f"Call or text from {_lead_url(lead['id'])}"
    parts = []
    if lead.get("phone_e164"):
        parts.append(f"Call {format_phone_display(lead['phone_e164'])}")
    if lead.get("email"):
        parts.append(lead["email"])
    return ". ".join(parts)


def _lead_url(lead_id: UUID) -> str:
    return f"{get_settings().base_url}/leads/{lead_id}"


def _rel(dt: datetime | None, now: datetime) -> str:
    if not dt:
        return "never"
    s = int((now - dt).total_seconds())
    if s < 3600:
        return f"{max(s // 60, 1)} min ago"
    if s < 86400:
        return f"{s // 3600} h ago"
    return f"{s // 86400} d ago"


CODES_SHORT = "Reply 1 called, 2 texted, 3 emailed, 4 snooze 1 day."
CODES_FULL = "Reply 1 called, 2 texted, 3 emailed, 4 snooze 1 day, 5 snooze 3 days."


def _area_note(lead: dict, tenant: dict, user: dict | None) -> str:
    flags = lead.get("flags") or {}
    if flags.get("auto_decline_at"):
        try:
            at = datetime.fromisoformat(flags["auto_decline_at"]).astimezone(tz_for(tenant)).strftime("%-I:%M %p")
        except ValueError:
            at = "15 minutes"
        note = f" OUTSIDE SERVICE AREA: an automatic 'we don't serve that area' message goes out at {at} unless you cancel"
        if user is not None:
            return note + f" here: {action_link(lead['id'], user['id'], 'keep_area')} "
        return note + "."
    if flags.get("possibly_out_of_area") or lead.get("area_status") == "out":
        return " POSSIBLY OUT OF AREA: check the address; decline from the lead page if so."
    if lead.get("area_status") == "near":
        return " Near the edge of the service area."
    return ""


def compose(rule: str, lead: dict, tenant: dict, payload: dict, now: datetime, user: dict | None = None) -> tuple[str, str, str]:
    """(sms_text, email_subject, email_text) for a client-facing reminder."""
    who = _lead_line(lead)
    contact = _contact_line(lead, tenant)
    url = _lead_url(lead["id"])
    src = PLATFORM_LABELS.get(Platform(lead.get("platform", "unknown")), "")
    if rule == "new_alert":
        area = _area_note(lead, tenant, user)
        missed = " MISSED CALL, call back now." if (lead.get("flags") or {}).get("missed_call") else ""
        sms = f"New lead: {who}, via {src}.{missed}{area} {contact}. {CODES_SHORT}"
        return sms, f"New lead: {who}", f"{who}\nSource: {src}{missed}{area}\n{contact}\n\n{(lead.get('message') or '').strip()}\n\nOpen: {url}"
    if rule == "after_appointment":
        when = lead.get("appointment_at")
        at = when.astimezone(tz_for(tenant)).strftime("%a %-I:%M %p") if when else "today"
        sms = f"How did the appointment with {who} ({at}) go? Mark it Quoted in the app, or reply W 8500 if won, L if lost, K to keep following up."
        return sms, f"After the appointment: {who}", f"The appointment with {who} was {at}. Update the lead: Quoted, Won (with the value) or Lost.\n\nOpen: {url}"
    if rule == "nudge":
        age = _rel(lead.get("created_at"), now)
        sms = f"Still waiting: {who} came in {age} and hasn't been contacted. {contact}. Reply 1/2/3 when you reach out."
        return sms, f"Not yet contacted: {who}", f"{who} arrived {age} and nobody has reached out yet.\n{contact}\n\nOpen: {url}"
    if rule == "cadence":
        last = _rel(lead.get("last_touch_at"), now)
        if payload.get("final"):
            sms = f"Last follow-up on our list for {who}, last touched {last}. {contact}. Reply W 8500 if won (with the value), L if lost, K to keep trying."
            return sms, f"Last follow-up: {who}", f"{who}: last touched {last}.\n{contact}\n\nIf it's done, mark it Won (with the value) or Lost, or keep trying.\nOpen: {url}"
        sms = f"Follow up with {who}, last touched {last}. {contact}. {CODES_FULL}"
        return sms, f"Follow up: {who}", f"{who}: last touched {last}.\n{contact}\n\nOpen: {url}"
    if rule == "reply_nudge":
        sms = f"{who} replied {_rel(lead.get('last_reply_at'), now)} and hasn't heard back. {contact}. Reply 2 when you've texted back."
        return sms, f"Waiting on you: {who}", f"{who} replied and hasn't heard back yet.\nThey said: {payload.get('text', '')}\n{contact}\n\nOpen: {url}"
    if rule == "replied":
        text = payload.get("text", "")
        sms = f"{who} replied: \"{text[:160]}\". Respond now. {contact}."
        return sms, f"{who} replied", f"{who} just replied:\n\n{text}\n\n{contact}\nOpen: {url}"
    if rule == "resubmit":
        sms = f"{who} submitted a form again. {contact}."
        return sms, f"Submitted again: {who}", f"{who} just submitted another form.\n{contact}\n\nOpen: {url}"
    if rule == "close_loop":
        sms = f"Did the quote for {who} close? Reply W 8500 (won, with the value) or L (lost)."
        return sms, f"Did it close? {who}", f"The quote for {who} went out a while ago. Mark it Won with the job value, or Lost.\n\nOpen: {url}"
    if rule == "snoozed":
        sms = f"Snooze over: follow up with {who}. {contact}. {CODES_FULL}"
        return sms, f"Snooze over: {who}", f"Time to follow up with {who}.\n{contact}\n\nOpen: {url}"
    return f"{who}: {contact}", who, f"{who}\n{contact}\n\nOpen: {url}"


def _escalation_text(lead: dict, tenant: dict, payload: dict, now: datetime) -> tuple[str, str, str]:
    who = _lead_line(lead)
    why = payload.get("why", "untouched")
    assignee = payload.get("assignee") or "nobody"
    if why == "ignored":
        head = f"{tenant['name']}: {who} has had {payload.get('ignored', 2)} follow-up reminders ignored"
    else:
        head = f"{tenant['name']}: {who} untouched for {_rel(lead.get('created_at'), now)}"
    sms = f"{head}. Assigned to {assignee}. {_lead_url(lead['id'])}"
    return sms, head, f"{head}.\nAssigned to: {assignee}\nSource: {lead.get('platform')}\n\n{_lead_url(lead['id'])}"


def action_link(lead_id: UUID, user_id: UUID, action: str) -> str:
    token = crypto.signed_token({"l": str(lead_id), "u": str(user_id), "a": action}, salt="act")
    return f"{get_settings().base_url}/act/{token}"


def _email_html(title: str, body_text: str, lead: dict, user: dict | None, rule: str) -> str:
    from html import escape

    lines = "".join(f"<p style='margin:0 0 10px;font-size:15px;line-height:1.45'>{escape(line)}</p>" for line in body_text.split("\n") if line.strip())
    buttons = ""
    flags = lead.get("flags") or {}
    if user is not None and rule == "new_alert" and flags.get("auto_decline_at"):
        buttons += f"<a href='{action_link(lead['id'], user['id'], 'keep_area')}' style='display:inline-block;margin:0 8px 8px 0;padding:11px 16px;background:#1d5fd1;color:#fff;font-weight:700;border-radius:6px;text-decoration:none'>Cancel the decline, we serve this</a>"
    elif user is not None and rule == "new_alert" and (flags.get("possibly_out_of_area") or lead.get("area_status") == "out"):
        buttons += f"<a href='{action_link(lead['id'], user['id'], 'decline_now')}' style='display:inline-block;margin:0 8px 8px 0;padding:11px 16px;background:#e6e8e3;color:#4f5a66;font-weight:700;border-radius:6px;text-decoration:none'>Decline: out of area</a>"
        buttons += f"<a href='{action_link(lead['id'], user['id'], 'keep_area')}' style='display:inline-block;margin:0 8px 8px 0;padding:11px 16px;background:#1d5fd1;color:#fff;font-weight:700;border-radius:6px;text-decoration:none'>It's in our area</a>"
    if user is not None and rule in ("new_alert", "nudge", "cadence", "reply_nudge", "snoozed", "replied", "resubmit"):
        acts = [("call", "I called"), ("text", "I texted"), ("email", "I emailed"), ("snooze1", "Snooze 1 day")]
        buttons = "".join(
            f"<a href='{action_link(lead['id'], user['id'], a)}' style='display:inline-block;margin:0 8px 8px 0;padding:11px 16px;background:#ffd400;color:#16202b;font-weight:700;border-radius:6px;text-decoration:none'>{label}</a>"
            for a, label in acts
        )
    if user is not None and rule in ("close_loop", "cadence"):
        buttons += f"<a href='{action_link(lead['id'], user['id'], 'won')}' style='display:inline-block;margin:0 8px 8px 0;padding:11px 16px;background:#dff3e6;color:#1f8a4c;font-weight:700;border-radius:6px;text-decoration:none'>Mark won</a>"
        buttons += f"<a href='{action_link(lead['id'], user['id'], 'lost')}' style='display:inline-block;margin:0 8px 8px 0;padding:11px 16px;background:#e6e8e3;color:#4f5a66;font-weight:700;border-radius:6px;text-decoration:none'>Mark lost</a>"
    return f"""<!doctype html><html><body style="margin:0;background:#eef0ec;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#16202b">
<div style="max-width:560px;margin:0 auto;padding:24px 16px">
  <div style="background:#16202b;color:#fff;padding:14px 18px;border-radius:8px 8px 0 0;font-weight:700">{escape(get_settings().agency_name)} CRM</div>
  <div style="background:#fff;padding:20px 18px;border:1px solid #d6d9d3;border-top:0;border-radius:0 0 8px 8px">
    <h2 style="margin:0 0 12px;font-size:19px">{escape(title)}</h2>
    {lines}
    <div style="margin-top:16px">{buttons}</div>
    <p style="margin:18px 0 0"><a href="{_lead_url(lead['id'])}" style="color:#1d5fd1">Open the lead</a></p>
  </div>
</div></body></html>"""


# --- Dispatch -------------------------------------------------------------------------------

@dataclass
class DispatchStats:
    sent: int = 0
    cancelled: int = 0
    escalations: int = 0


def _send_to_user(conn: psycopg.Connection, user: dict, tenant: dict, lead: dict, reminder_id: UUID, kind: str, sms: str, subject: str, text: str, html: str) -> bool:
    any_ok = False
    if user.get("notify_sms") and user.get("mobile_e164"):
        # Reminders to clients are the agency talking; they come from the agency number. The client's own
        # number is reserved for conversations with leads.
        r = messaging.send_sms(conn, to=user["mobile_e164"], body=sms, kind=kind, tenant_id=tenant["id"], lead_id=lead["id"], user_id=user["id"], reminder_id=reminder_id, from_=get_settings().twilio_agency_from)
        any_ok = any_ok or r.ok
    if user.get("notify_email") or (not any_ok and user.get("email")):
        r = messaging.send_email(conn, to=user["email"], subject=subject, text=text, html=html, kind=kind, tenant_id=tenant["id"], lead_id=lead["id"], user_id=user["id"], reminder_id=reminder_id)
        any_ok = any_ok or r.ok
    return any_ok


def relay_enabled(tenant: dict) -> bool:
    """Lead texts are forwarded from the client's number to the assignee's phone, and answers relayed back."""
    sms_cfg = (tenant.get("settings") or {}).get("sms") or {}
    return bool(tenant.get("twilio_number")) and sms_cfg.get("relay_replies", True)


def relay_text(lead: dict, text: str) -> str:
    name = lead.get("contact_name") or format_phone_display(lead.get("phone_e164")) or "Lead"
    return f"[{lead['ref_code']}] {name}: {text}\n\nReply to this text to answer them. Codes 1-5, W, L, K, N still work."


def _relay_to_user(conn: psycopg.Connection, user: dict, tenant: dict, lead: dict, reminder_id: UUID, text: str, subject: str, email_text: str, html: str) -> bool:
    any_ok = False
    if user.get("notify_sms") and user.get("mobile_e164"):
        r = messaging.send_sms(conn, to=user["mobile_e164"], body=relay_text(lead, text), kind="relay", tenant_id=tenant["id"], lead_id=lead["id"], user_id=user["id"], reminder_id=reminder_id, from_=tenant["twilio_number"])
        any_ok = any_ok or r.ok
    if user.get("notify_email") or (not any_ok and user.get("email")):
        r = messaging.send_email(conn, to=user["email"], subject=subject, text=email_text, html=html, kind="alert", tenant_id=tenant["id"], lead_id=lead["id"], user_id=user["id"], reminder_id=reminder_id)
        any_ok = any_ok or r.ok
    return any_ok


# Something a client would type as a reply code, as opposed to a sentence meant for the lead.
_STRICT_CODE = re.compile(r"^\s*(?:[A-Za-z]\d{3}\s+)?(?:[1-5]|[Ww]\s*[\d,.$]+|[Ll](?:\s+\w+)?|[Kk]|[Nn]\s+.+|\?)\s*$", re.S)


def looks_like_code(text: str) -> bool:
    return bool(_STRICT_CODE.match(text or ""))


def relay_target(conn: psycopg.Connection, user: dict, tenant: dict, text: str) -> tuple[dict | None, str]:
    """Which lead a client's free-text message is for: the ref it starts with, else the lead whose text was
    most recently relayed to this user. Returns (lead, message_without_ref)."""
    m = re.match(r"^\s*([A-Za-z]\d{3})\b[\s:,-]*(.*)$", text or "", re.S)
    if m:
        lead = conn.execute("SELECT * FROM lead WHERE tenant_id = %s AND ref_code = %s", (tenant["id"], m.group(1).upper())).fetchone()
        if lead is not None:
            return lead, m.group(2).strip()
    last = conn.execute(
        "SELECT lead_id FROM message_log WHERE user_id = %s AND kind = 'relay' AND direction = 'out' AND lead_id IS NOT NULL ORDER BY created_at DESC LIMIT 1",
        (user["id"],),
    ).fetchone()
    if last is None:
        return None, text
    return conn.execute("SELECT * FROM lead WHERE id = %s", (last["lead_id"],)).fetchone(), text


def dispatch_due(conn: psycopg.Connection, *, now: datetime | None = None, lead_id: UUID | None = None, limit: int = 200) -> DispatchStats:
    now = now or datetime.now(UTC)
    stats = DispatchStats()
    rows = conn.execute(
        """
        SELECT r.* FROM reminder r
        WHERE r.sent_at IS NULL AND r.cancelled_at IS NULL AND r.due_at <= %s
          AND (%s::uuid IS NULL OR r.lead_id = %s)
        ORDER BY r.due_at
        LIMIT %s
        FOR UPDATE SKIP LOCKED
        """,
        (now, lead_id, lead_id, limit),
    ).fetchall()
    for r in rows:
        lead = conn.execute("SELECT l.*, u.name AS assignee_name FROM lead l LEFT JOIN app_user u ON u.id = l.assigned_to WHERE l.id = %s", (r["lead_id"],)).fetchone()
        tenant = conn.execute("SELECT * FROM tenant WHERE id = %s", (r["tenant_id"],)).fetchone() if lead else None
        if lead is None or tenant is None or not tenant["is_active"]:
            conn.execute("UPDATE reminder SET cancelled_at = now() WHERE id = %s", (r["id"],))
            stats.cancelled += 1
            continue
        rule = r["rule"]
        payload = r["payload"] or {}
        closed = lead["status"] in (LeadStatus.WON.value, LeadStatus.LOST.value)
        if closed and rule != "replied":
            conn.execute("UPDATE reminder SET cancelled_at = now() WHERE id = %s", (r["id"],))
            stats.cancelled += 1
            continue
        if rule in ("nudge",) and lead["status"] != LeadStatus.NEW.value:
            conn.execute("UPDATE reminder SET cancelled_at = now() WHERE id = %s", (r["id"],))
            stats.cancelled += 1
            continue
        if rule in LEAD_FACING_RULES:
            from app.services import lead_messages

            lead_messages.dispatch(conn, r, tenant, lead, now=now)
            conn.execute("UPDATE reminder SET sent_at = %s WHERE id = %s", (now, r["id"]))
            stats.sent += 1
            continue
        if rule == "escalation":
            if payload.get("why") == "untouched" and (lead["status"] != LeadStatus.NEW.value or lead["last_touch_at"]):
                conn.execute("UPDATE reminder SET cancelled_at = now() WHERE id = %s", (r["id"],))
                stats.cancelled += 1
                continue
            payload = {**payload, "assignee": lead.get("assignee_name")}
            sms, subject, text = _escalation_text(lead, tenant, payload, now)
            html = _email_html(subject, text, lead, None, rule)
            sent_any = False
            for admin in _agency_recipients(conn):
                sent_any = _send_to_user(conn, admin, tenant, lead, r["id"], "escalation", sms, subject, text, html) or sent_any
            conn.execute("UPDATE reminder SET sent_at = %s, escalated_at = %s WHERE id = %s", (now, now, r["id"]))
            lead_svc.log_activity(conn, tenant["id"], lead["id"], ActivityType.ESCALATION, body=subject, detail=payload, occurred_at=now)
            stats.escalations += 1
            continue

        recipients = _recipients(conn, tenant["id"], lead.get("assigned_to"))
        sent_any = False
        sms, subject, text = compose(rule, lead, tenant, payload, now)
        relay = rule == "replied" and relay_enabled(tenant)
        for user in recipients:
            sms, subject, text = compose(rule, lead, tenant, payload, now, user)
            html = _email_html(subject, text, lead, user, rule)
            if relay:
                # The lead's words, from the client's own number, so the client can answer from their phone
                # and the reply comes straight back through the CRM to the lead.
                sent_any = _relay_to_user(conn, user, tenant, lead, r["id"], payload.get("text", ""), subject, text, html) or sent_any
                continue
            sent_any = _send_to_user(conn, user, tenant, lead, r["id"], "alert" if rule in ("new_alert", "replied", "resubmit") else "reminder", sms, subject, text, html) or sent_any
        conn.execute("UPDATE reminder SET sent_at = %s WHERE id = %s", (now, r["id"]))
        if rule in ("nudge", "cadence", "reply_nudge", "close_loop", "snoozed", "after_appointment"):
            lead_svc.log_activity(conn, tenant["id"], lead["id"], ActivityType.REMINDER_SENT, body=subject, detail={"rule": rule, **payload, "delivered": sent_any}, occurred_at=now)
        if rule == "cadence":
            conn.execute("UPDATE lead SET next_action_at = %s WHERE id = %s", (now, lead["id"]))
            _maybe_escalate_ignored(conn, tenant, lead, now)
        stats.sent += 1
    return stats


def _maybe_escalate_ignored(conn: psycopg.Connection, tenant: dict, lead: dict, now: datetime) -> None:
    cfg = settings_for(tenant)
    base = lead.get("cadence_base")
    ignored = conn.execute(
        """
        SELECT count(*) AS n FROM reminder
        WHERE lead_id = %s AND rule = 'cadence' AND sent_at IS NOT NULL AND acknowledged_at IS NULL
          AND (%s::timestamptz IS NULL OR due_at >= %s)
        """,
        (lead["id"], base, base),
    ).fetchone()["n"]
    if ignored >= int(cfg["ignored_before_escalation"]):
        key = f"{lead['id']}:escalation:ignored:{_ts(base or now)}"
        schedule(conn, tenant_id=tenant["id"], lead_id=lead["id"], user_id=None, rule="escalation", due_at=now, key=key, payload={"why": "ignored", "ignored": int(ignored)})


# --- Digests --------------------------------------------------------------------------------

NEEDS_ACTION = """
    NOT (l.flags ? 'needs_review') AND l.status NOT IN ('won', 'lost') AND (
        l.status = 'new'
        OR (l.status IN ('contacted', 'no_response') AND COALESCE(l.last_touch_at, l.created_at) < %s - interval '1 day')
        OR (l.status = 'quoted' AND COALESCE(l.quoted_at, l.created_at) < %s - interval '7 days')
        OR (l.next_action_at IS NOT NULL AND l.next_action_at <= %s)
    )
"""


def run_digests(conn: psycopg.Connection, *, now: datetime | None = None) -> int:
    """Once per working day per tenant, after the digest time: a list of leads that need action, per user."""
    now = now or datetime.now(UTC)
    sent = 0
    for tenant in conn.execute("SELECT * FROM tenant WHERE is_active").fetchall():
        cfg = settings_for(tenant)
        tz = tz_for(tenant)
        local = now.astimezone(tz)
        hours = tenant.get("business_hours") or {}
        days = {int(d) for d in hours.get("days", [1, 2, 3, 4, 5, 6])}
        if local.isoweekday() not in days or local.time() < _hm(cfg["digest_time"], time(7, 30)):
            continue
        day_start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
        for user in conn.execute("SELECT * FROM app_user WHERE tenant_id = %s AND is_active AND (notify_sms OR notify_email)", (tenant["id"],)).fetchall():
            already = conn.execute(
                "SELECT 1 FROM message_log WHERE tenant_id = %s AND user_id = %s AND kind = 'digest' AND created_at >= %s", (tenant["id"], user["id"], day_start)
            ).fetchone()
            if already:
                continue
            where = NEEDS_ACTION
            params: list[Any] = [now, now, now, tenant["id"]]
            sql = f"SELECT l.* FROM lead l WHERE {where} AND l.tenant_id = %s"
            if not user.get("see_all_leads", True) and user["role"] != Role.OWNER.value:
                sql += " AND l.assigned_to = %s"
                params.append(user["id"])
            sql += " ORDER BY l.created_at ASC LIMIT 50"
            due = conn.execute(sql, params).fetchall()
            if not due:
                continue
            lines = [f"- {_lead_line(ld)}: {ld['status'].replace('_', ' ')}, {'arrived' if ld['status'] == 'new' else 'last touched'} {_rel(ld.get('last_touch_at') or ld.get('created_at'), now)}. {_contact_line(ld, tenant)}" for ld in due]
            subject = f"{len(due)} lead{'s' if len(due) != 1 else ''} need{'s' if len(due) == 1 else ''} a follow-up today"
            text = f"Good morning. {subject}:\n\n" + "\n".join(lines) + f"\n\nOpen your leads: {get_settings().base_url}/leads"
            html = _digest_html(subject, due, now, tenant)
            sms = f"{subject}: " + "; ".join(_lead_line(ld) for ld in due[:3]) + (f" and {len(due) - 3} more" if len(due) > 3 else "") + f". {get_settings().base_url}/leads"
            if user.get("notify_email"):
                messaging.send_email(conn, to=user["email"], subject=subject, text=text, html=html, kind="digest", tenant_id=tenant["id"], user_id=user["id"])
            if user.get("notify_sms") and user.get("mobile_e164"):
                messaging.send_sms(conn, to=user["mobile_e164"], body=sms, kind="digest", tenant_id=tenant["id"], user_id=user["id"])
            sent += 1
    sent += _agency_digest(conn, now)
    return sent


def _digest_html(subject: str, due: list[dict], now: datetime, tenant: dict | None = None) -> str:
    from html import escape

    rows = "".join(
        f"<tr><td style='padding:8px 10px;border-bottom:1px solid #e6e8e3'><a href='{_lead_url(ld['id'])}' style='color:#16202b;font-weight:700;text-decoration:none'>{escape(_lead_line(ld))}</a><br><span style='color:#4f5a66;font-size:13px'>{escape(ld['status'].replace('_', ' '))}, {'arrived' if ld['status'] == 'new' else 'last touched'} {escape(_rel(ld.get('last_touch_at') or ld.get('created_at'), now))}. {escape(_contact_line(ld, tenant))}</span></td></tr>"
        for ld in due
    )
    return f"""<!doctype html><html><body style="margin:0;background:#eef0ec;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#16202b">
<div style="max-width:560px;margin:0 auto;padding:24px 16px">
  <div style="background:#16202b;color:#fff;padding:14px 18px;border-radius:8px 8px 0 0;font-weight:700">{escape(get_settings().agency_name)} CRM</div>
  <div style="background:#fff;padding:20px 18px;border:1px solid #d6d9d3;border-top:0;border-radius:0 0 8px 8px">
    <h2 style="margin:0 0 12px;font-size:19px">{escape(subject)}</h2>
    <table style="width:100%;border-collapse:collapse">{rows}</table>
    <p style="margin:18px 0 0"><a href="{get_settings().base_url}/leads" style="display:inline-block;padding:11px 16px;background:#ffd400;color:#16202b;font-weight:700;border-radius:6px;text-decoration:none">Open your leads</a></p>
  </div>
</div></body></html>"""


def _agency_digest(conn: psycopg.Connection, now: datetime) -> int:
    admins = [a for a in _agency_recipients(conn) if a.get("notify_email")]
    if not admins:
        return 0
    tz = ZoneInfo("America/Toronto")
    local = now.astimezone(tz)
    if local.isoweekday() == 7 or local.time() < time(7, 30):
        return 0
    day_start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    rows = conn.execute(
        f"""
        SELECT t.name,
               count(*) FILTER (WHERE {NEEDS_ACTION.replace('%s', '%(now)s')}) AS needs_action,
               count(*) FILTER (WHERE l.status = 'new' AND l.created_at < %(now)s - interval '24 hours') AS stale_new,
               count(*) FILTER (WHERE l.created_at >= %(now)s - interval '24 hours') AS new_24h
        FROM tenant t LEFT JOIN lead l ON l.tenant_id = t.id
        WHERE t.is_active
        GROUP BY t.id, t.name ORDER BY stale_new DESC, needs_action DESC, t.name
        """,
        {"now": now},
    ).fetchall()
    if not rows or not any(r["needs_action"] or r["new_24h"] for r in rows):
        return 0
    subject = f"Agency digest: {sum(r['stale_new'] for r in rows)} new leads untouched over 24 h across {len(rows)} clients"
    lines = [f"- {r['name']}: {r['new_24h']} new in 24 h, {r['needs_action']} need action, {r['stale_new']} new untouched >24 h" for r in rows]
    text = subject + "\n\n" + "\n".join(lines) + f"\n\n{get_settings().base_url}/admin"
    sent = 0
    for admin in admins:
        if conn.execute("SELECT 1 FROM message_log WHERE user_id = %s AND kind = 'agency_digest' AND created_at >= %s", (admin["id"], day_start)).fetchone():
            continue
        messaging.send_email(conn, to=admin["email"], subject=subject, text=text, html=None, kind="agency_digest", tenant_id=None, user_id=admin["id"])
        sent += 1
    return sent


# --- Reply codes (SMS replies and email action links) -----------------------------------------

_CODE = re.compile(r"^\s*(?:(?P<ref>[A-Za-z]\d{3})\s+)?(?P<code>[0-9]|[WLKNwlkn])\b\s*(?P<rest>.*)$", re.S)

LOST_BY_NUMBER = {str(i + 1): r for i, r in enumerate(LostReason)}
LOST_BY_WORD = {r.value: r for r in LostReason} | {"price": LostReason.PRICE, "timing": LostReason.TIMING, "spam": LostReason.SPAM, "area": LostReason.OUT_OF_AREA, "elsewhere": LostReason.WENT_ELSEWHERE, "noresponse": LostReason.NO_RESPONSE, "no": LostReason.NO_RESPONSE}


@dataclass
class ReplyOutcome:
    reply: str
    lead_id: UUID | None = None
    handled: bool = False


def apply_reply_code(conn: psycopg.Connection, user: dict, text: str, *, now: datetime | None = None) -> ReplyOutcome:
    """A client texted a code back. Figure out which lead, do the thing, say what was done."""
    now = now or datetime.now(UTC)
    m = _CODE.match(text or "")
    if not m:
        return ReplyOutcome("Codes: 1 called, 2 texted, 3 emailed, 4 snooze 1 day, 5 snooze 3 days, W 8500 won, L lost, K keep trying, N note. Start with the ref (e.g. A14 1) to pick a lead.")
    ref, code, rest = (m.group("ref") or "").upper(), m.group("code").upper(), (m.group("rest") or "").strip()
    tenant = conn.execute("SELECT * FROM tenant WHERE id = %s", (user["tenant_id"],)).fetchone() if user.get("tenant_id") else None
    if tenant is None:
        return ReplyOutcome("Reply codes work for client logins only.")
    lead = None
    if ref:
        lead = conn.execute("SELECT * FROM lead WHERE tenant_id = %s AND ref_code = %s", (tenant["id"], ref)).fetchone()
        if lead is None:
            return ReplyOutcome(f"No lead {ref} on your account.")
    else:
        last = conn.execute(
            "SELECT lead_id FROM reminder WHERE user_id = %s AND sent_at IS NOT NULL ORDER BY sent_at DESC LIMIT 1", (user["id"],)
        ).fetchone()
        if last is None:
            last = conn.execute("SELECT lead_id FROM reminder r JOIN lead l ON l.id = r.lead_id WHERE l.tenant_id = %s AND r.sent_at IS NOT NULL ORDER BY r.sent_at DESC LIMIT 1", (tenant["id"],)).fetchone()
        if last is None:
            return ReplyOutcome("Which lead? Start with its ref, for example: A14 1")
        lead = conn.execute("SELECT * FROM lead WHERE id = %s", (last["lead_id"],)).fetchone()
    if lead is None:
        return ReplyOutcome("Which lead? Start with its ref, for example: A14 1")
    name = lead.get("contact_name") or lead["ref_code"]
    label = user["name"]
    if code in ("1", "2", "3"):
        kind = {"1": ActivityType.CALL_ATTEMPT, "2": ActivityType.TEXT_SENT, "3": ActivityType.EMAIL_SENT}[code]
        lead_svc.log_touch(conn, lead, kind, actor_id=user["id"], actor_label=label, note=rest or None, now=now)
        verb = {"1": "called", "2": "texted", "3": "emailed"}[code]
        return ReplyOutcome(f"Logged: {verb} {name} ({lead['ref_code']}). Next reminder in 1 day if nothing else is logged.", lead["id"], True)
    if code in ("4", "5"):
        days = 1 if code == "4" else 3
        until = on_snooze(conn, tenant, lead, days, now=now)
        return ReplyOutcome(f"Snoozed {name} ({lead['ref_code']}) until {until.astimezone(tz_for(tenant)).strftime('%a %-I:%M %p')}.", lead["id"], True)
    if code == "W":
        digits = re.sub(r"[^0-9.]", "", rest.replace(",", ""))
        if not digits:
            return ReplyOutcome(f"Reply W with the job value, for example: {lead['ref_code']} W 8500", lead["id"])
        value = Decimal(digits)
        lead_svc.change_status(conn, lead, LeadStatus.WON, actor_id=user["id"], actor_label=label, deal_value=value, now=now)
        return ReplyOutcome(f"{name} ({lead['ref_code']}) marked Won at ${value:,.0f}. Nice.", lead["id"], True)
    if code == "L":
        word = rest.split()[0].lower() if rest else ""
        reason = LOST_BY_NUMBER.get(word) or LOST_BY_WORD.get(word.replace("_", "").replace("-", "")) or LostReason.OTHER
        lead_svc.change_status(conn, lead, LeadStatus.LOST, actor_id=user["id"], actor_label=label, lost_reason=reason, now=now)
        return ReplyOutcome(f"{name} ({lead['ref_code']}) marked Lost ({reason.value.replace('_', ' ')}). Reminders stopped.", lead["id"], True)
    if code == "K":
        on_keep_trying(conn, tenant, lead, now=now)
        return ReplyOutcome(f"Keeping at it: {name} ({lead['ref_code']}) gets another round of reminders.", lead["id"], True)
    if code == "N":
        if not rest:
            return ReplyOutcome("Reply N followed by your note, for example: N left voicemail", lead["id"])
        lead_svc.add_note(conn, lead, rest, actor_id=user["id"], actor_label=label)
        acknowledge(conn, lead["id"])
        return ReplyOutcome(f"Note added to {name} ({lead['ref_code']}).", lead["id"], True)
    return ReplyOutcome("Codes: 1 called, 2 texted, 3 emailed, 4 snooze 1 day, 5 snooze 3 days, W 8500 won, L lost, K keep trying, N note.", lead["id"])


def apply_action(conn: psycopg.Connection, user: dict, lead: dict, action: str, *, now: datetime | None = None, deal_value: Decimal | None = None, lost_reason: LostReason | None = None) -> str:
    """Email action links. Returns a confirmation sentence."""
    now = now or datetime.now(UTC)
    tenant = conn.execute("SELECT * FROM tenant WHERE id = %s", (lead["tenant_id"],)).fetchone()
    name = lead.get("contact_name") or lead["ref_code"]
    if action in ("call", "text", "email"):
        kind = {"call": ActivityType.CALL_ATTEMPT, "text": ActivityType.TEXT_SENT, "email": ActivityType.EMAIL_SENT}[action]
        lead_svc.log_touch(conn, lead, kind, actor_id=user["id"], actor_label=user["name"], now=now)
        return f"Logged: {action}ed {name}." if action != "call" else f"Logged: called {name}."
    if action in ("snooze1", "snooze3"):
        until = on_snooze(conn, tenant, lead, 1 if action == "snooze1" else 3, now=now)
        return f"Snoozed {name} until {until.astimezone(tz_for(tenant)).strftime('%A %-I:%M %p')}."
    if action == "won":
        if deal_value is None:
            raise ValueError("value required")
        lead_svc.change_status(conn, lead, LeadStatus.WON, actor_id=user["id"], actor_label=user["name"], deal_value=deal_value, now=now)
        return f"{name} marked Won at ${deal_value:,.0f}."
    if action == "lost":
        lead_svc.change_status(conn, lead, LeadStatus.LOST, actor_id=user["id"], actor_label=user["name"], lost_reason=lost_reason or LostReason.OTHER, now=now)
        return f"{name} marked Lost."
    if action == "keep":
        on_keep_trying(conn, tenant, lead, now=now)
        return f"Another round of reminders for {name}."
    from app.services import lead_messages

    if action == "keep_area":
        return cancel_auto_decline_text(lead_messages.cancel_auto_decline(conn, tenant, lead, actor_id=user["id"], actor_label=user["name"], now=now), name)
    if action == "decline_now":
        return lead_messages.decline_now(conn, tenant, lead, actor_id=user["id"], actor_label=user["name"], now=now)
    raise ValueError("unknown action")


def cancel_auto_decline_text(message: str, name: str) -> str:
    return f"{name}: {message}"

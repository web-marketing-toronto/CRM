"""What happened on a touch, and what's next.

After a call, text or email the client taps one outcome (spoke to them, voicemail, booked a visit, sent a
quote, not interested...) and, where it matters, one next step (call back Thursday, site visit Tuesday
2:30, waiting on them until Friday). The next step is the only thing the lead remembers about its
future: it is shown on the lead and in the list, it is what the reminder says, and a lead whose next
step is due is what Needs action means. Appointments are one kind of next step; which kind a client
books (site visits, calls, or neither) is a per-client setting.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg

from app.models import ActivityType, LeadStatus, LostReason
from app.services import followup
from app.services import leads as lead_svc

# --- Per-client appointment kind -------------------------------------------------------------------

APPOINTMENT_KINDS = {
    "visit": {"label": "Site visit", "booked": "Booked a site visit", "verb": "will be there", "next": "Site visit", "prompt": "How did the visit go?"},
    "call": {"label": "Call", "booked": "Booked a call", "verb": "will call you", "next": "Scheduled call", "prompt": "How did the call go?"},
    "off": {"label": "Off", "booked": "", "verb": "", "next": "", "prompt": ""},
}


def appointment_kind(tenant: dict) -> str:
    kind = (((tenant.get("settings") or {}).get("appointments") or {}).get("kind")) or "visit"
    return kind if kind in APPOINTMENT_KINDS else "visit"


def appointment_words(tenant: dict) -> dict[str, str]:
    return APPOINTMENT_KINDS[appointment_kind(tenant)]


# --- Vocabulary ------------------------------------------------------------------------------------

OUTCOMES: dict[str, str] = {
    "spoke": "Spoke to them",
    "voicemail": "Left a voicemail",
    "no_answer": "No answer",
    "no_reply": "No reply yet",
    "booked": "Booked",            # label comes from the client's appointment kind
    "quoted": "Sent a quote",
    "not_interested": "Not interested",
    "wrong_number": "Wrong number",
    "spam": "Spam",
}

NEXT_STEPS: dict[str, str] = {
    "call_back": "Call back",
    "appointment": "Appointment",  # label comes from the client's appointment kind
    "send_quote": "Send a quote",
    "waiting": "Waiting on them",
}

# Outcomes that end the lead, and the reason recorded.
CLOSING: dict[str, LostReason] = {"not_interested": LostReason.OTHER, "wrong_number": LostReason.SPAM, "spam": LostReason.SPAM}


def outcome_label(tenant: dict, outcome: str) -> str:
    if outcome == "booked":
        return appointment_words(tenant)["booked"] or "Booked"
    return OUTCOMES.get(outcome, outcome)


def step_label(tenant: dict, step: str | None) -> str:
    if not step:
        return ""
    if step == "appointment":
        return appointment_words(tenant)["next"] or "Appointment"
    return NEXT_STEPS.get(step, step)


def choices(tenant: dict) -> dict[str, Any]:
    """What the lead page offers, in the client's words."""
    kind = appointment_kind(tenant)
    outcomes = [(k, outcome_label(tenant, k)) for k in OUTCOMES if not (k == "booked" and kind == "off")]
    steps = [(k, step_label(tenant, k)) for k in NEXT_STEPS if not (k == "appointment" and kind == "off")]
    return {"kind": kind, "outcomes": outcomes, "steps": steps, "words": appointment_words(tenant)}


# --- When ------------------------------------------------------------------------------------------

QUICK_WHEN = ("later_today", "tomorrow", "in_2_days", "next_week")


def resolve_when(tenant: dict, value: str | None, *, now: datetime | None = None) -> datetime | None:
    """A quick choice ('tomorrow') or an ISO datetime typed in the client's timezone -> UTC datetime."""
    now = now or datetime.now(UTC)
    tz = followup.tz_for(tenant)
    local = now.astimezone(tz)
    value = (value or "").strip()
    if not value:
        return None
    if value == "later_today":
        return followup.next_business_time(now + timedelta(hours=3), tenant)
    if value == "tomorrow":
        return followup.next_business_time(datetime.combine(local.date() + timedelta(days=1), time(9, 0), tz).astimezone(UTC), tenant)
    if value == "in_2_days":
        return followup.next_business_time(datetime.combine(local.date() + timedelta(days=2), time(9, 0), tz).astimezone(UTC), tenant)
    if value == "next_week":
        days = (7 - local.weekday()) % 7 or 7  # next Monday
        return followup.next_business_time(datetime.combine(local.date() + timedelta(days=days), time(9, 0), tz).astimezone(UTC), tenant)
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(UTC)


def pretty_when(tenant: dict, at: datetime | None, *, now: datetime | None = None) -> str:
    if at is None:
        return ""
    now = now or datetime.now(UTC)
    tz = followup.tz_for(tenant)
    local, today = at.astimezone(tz), now.astimezone(tz).date()
    delta = (local.date() - today).days
    day = "today" if delta == 0 else "tomorrow" if delta == 1 else "yesterday" if delta == -1 else local.strftime("%a %b %-d")
    return f"{day} {local.strftime('%-I:%M %p')}"


# --- Next step --------------------------------------------------------------------------------------

def set_next_step(
    conn: psycopg.Connection, tenant: dict, lead: dict, step: str | None, at: datetime | None, *,
    actor_id: UUID | None, actor_label: str, note: str | None = None, now: datetime | None = None, log: bool = True,
) -> str:
    """Record the one thing that happens next and schedule the reminder for it. `None` clears it."""
    now = now or datetime.now(UTC)
    lid = lead["id"]
    if step is None:
        conn.execute("UPDATE lead SET next_step = NULL, next_step_at = NULL, next_step_note = NULL, snoozed_until = NULL WHERE id = %s", (lid,))
        followup.cancel(conn, lid, ("next_step", "snoozed"))
        if log:
            lead_svc.log_activity(conn, tenant["id"], lid, ActivityType.NOTE, actor_id=actor_id, actor_label=actor_label, body="Next step cleared", occurred_at=now)
        return "Next step cleared."
    if step not in NEXT_STEPS:
        raise lead_svc.LeadError("unknown next step")
    if step == "appointment":
        from app.services import lead_messages

        if at is None:
            raise lead_svc.LeadError("Pick a date and time")
        return lead_messages.set_appointment(conn, tenant, lead, at, actor_id=actor_id, actor_label=actor_label, now=now)
    at = at or followup.next_business_time(now + timedelta(days=1), tenant)
    conn.execute(
        "UPDATE lead SET next_step = %s, next_step_at = %s, next_step_note = %s, next_action_at = %s, snoozed_until = %s, appointment_at = NULL WHERE id = %s",
        (step, at, (note or "")[:300] or None, at, at if step == "waiting" else None, lid),
    )
    # The planned step replaces the generic cadence until it comes due; the escalation for an untouched
    # new lead no longer applies because someone has clearly engaged.
    followup.cancel(conn, lid, ("nudge", "cadence", "reply_nudge", "snoozed", "close_loop", "next_step", "appointment", "after_appointment", "escalation"))
    followup.acknowledge(conn, lid)
    followup.schedule(conn, tenant_id=tenant["id"], lead_id=lid, user_id=lead.get("assigned_to"), rule="next_step", due_at=at, key=f"{lid}:next_step:{followup._ts(at)}:{step}", payload={"step": step, "note": note or ""})
    label = step_label(tenant, step)
    when = pretty_when(tenant, at, now=now)
    if log:
        lead_svc.log_activity(conn, tenant["id"], lid, ActivityType.NOTE, actor_id=actor_id, actor_label=actor_label, body=f"Next: {label.lower()} {when}{(' — ' + note) if note else ''}", detail={"next_step": step, "at": at.isoformat()}, occurred_at=now)
    return f"Next: {label.lower()} {when}."


# --- Outcomes -------------------------------------------------------------------------------------

def record(
    conn: psycopg.Connection, tenant: dict, lead: dict, outcome: str, *,
    actor_id: UUID | None, actor_label: str, next_step: str | None = None, at: datetime | None = None,
    note: str | None = None, value: Decimal | None = None, lost_reason: LostReason | None = None, now: datetime | None = None,
    quiet_step: bool = False,
) -> str:
    """Apply one outcome. Returns a short confirmation. May raise LeadError when something required is missing."""
    now = now or datetime.now(UTC)
    if outcome not in OUTCOMES:
        raise lead_svc.LeadError("unknown outcome")
    if outcome == "booked" and appointment_kind(tenant) == "off":
        raise lead_svc.LeadError("Appointments are turned off for this account")
    lid, tid = lead["id"], tenant["id"]
    label = outcome_label(tenant, outcome)
    lead_svc.log_activity(conn, tid, lid, ActivityType.OUTCOME, actor_id=actor_id, actor_label=actor_label, body=label + (f" — {note}" if note else ""), detail={"outcome": outcome, "next_step": next_step, "at": at.isoformat() if at else None}, occurred_at=now)
    status = lead["status"]

    if outcome in CLOSING:
        reason = lost_reason or CLOSING[outcome]
        lead_svc.change_status(conn, lead, LeadStatus.LOST, actor_id=actor_id, actor_label=actor_label, lost_reason=reason, note=None, now=now)
        conn.execute("UPDATE lead SET next_step = NULL, next_step_at = NULL, next_step_note = NULL WHERE id = %s", (lid,))
        return f"{label}: marked Lost, reminders stopped."

    if outcome == "booked":
        if at is None:
            raise lead_svc.LeadError("Pick the date and time")
        fresh = lead_svc.get_lead(conn, lid) or lead
        msg = set_next_step(conn, tenant, fresh, "appointment", at, actor_id=actor_id, actor_label=actor_label, now=now)
        return msg

    if outcome == "quoted":
        if status not in (LeadStatus.QUOTED.value, LeadStatus.WON.value):
            lead_svc.change_status(conn, lead, LeadStatus.QUOTED, actor_id=actor_id, actor_label=actor_label, deal_value=value, note=None, now=now)
        fresh = lead_svc.get_lead(conn, lid) or lead
        step = next_step or "call_back"
        when = at or followup.next_business_time(now + timedelta(days=3), tenant)
        set_next_step(conn, tenant, fresh, step, when, actor_id=actor_id, actor_label=actor_label, note=note or "follow up on the quote", now=now)
        return f"Quoted. Next: {step_label(tenant, step).lower()} {pretty_when(tenant, when, now=now)}."

    if outcome == "spoke":
        if status in (LeadStatus.NEW.value, LeadStatus.NO_RESPONSE.value):
            _mark_contacted(conn, tenant, lead, actor_id, actor_label, now)
        if not next_step:
            raise lead_svc.LeadError("What's next? Pick a next step so the lead doesn't go cold.")
        fresh = lead_svc.get_lead(conn, lid) or lead
        return "Spoke to them. " + set_next_step(conn, tenant, fresh, next_step, at, actor_id=actor_id, actor_label=actor_label, note=note, now=now, log=not quiet_step)

    # voicemail / no_answer / no_reply: keep trying, on a schedule the client can see.
    if status in (LeadStatus.NEW.value, LeadStatus.CONTACTED.value):
        _set_status(conn, tenant, lead, LeadStatus.NO_RESPONSE, actor_id, actor_label, now, because=label)
    fresh = lead_svc.get_lead(conn, lid) or lead
    step = next_step or "call_back"
    when = at or followup.next_business_time(now + timedelta(hours=4), tenant)
    return f"{label}. " + set_next_step(conn, tenant, fresh, step, when, actor_id=actor_id, actor_label=actor_label, note=note, now=now)


def _mark_contacted(conn: psycopg.Connection, tenant: dict, lead: dict, actor_id: UUID | None, actor_label: str, now: datetime) -> None:
    sets = ["status = 'contacted'", "last_touch_at = %s"]
    params: list[Any] = [now]
    if lead.get("first_contact_at") is None:
        sets.append("first_contact_at = %s")
        params.append(now)
    params.append(lead["id"])
    conn.execute(f"UPDATE lead SET {', '.join(sets)} WHERE id = %s", params)
    lead_svc.log_activity(conn, tenant["id"], lead["id"], ActivityType.STATUS_CHANGE, actor_id=actor_id, actor_label=actor_label, detail={"from": lead["status"], "to": "contacted", "auto": True, "because": "spoke to them"}, occurred_at=now)
    if lead["status"] == LeadStatus.NEW.value:
        from app.services import conversions

        conversions.on_status_change(conn, tenant, lead, LeadStatus.NEW, LeadStatus.CONTACTED, now=now)


def _set_status(conn: psycopg.Connection, tenant: dict, lead: dict, new: LeadStatus, actor_id: UUID | None, actor_label: str, now: datetime, *, because: str) -> None:
    if lead["status"] == new.value:
        return
    conn.execute("UPDATE lead SET status = %s, last_touch_at = coalesce(last_touch_at, %s) WHERE id = %s", (new.value, now, lead["id"]))
    lead_svc.log_activity(conn, tenant["id"], lead["id"], ActivityType.STATUS_CHANGE, actor_id=actor_id, actor_label=actor_label, detail={"from": lead["status"], "to": new.value, "auto": True, "because": because.lower()}, occurred_at=now)


# --- For the page -----------------------------------------------------------------------------------

def awaiting_outcome(activity: list[dict], *, now: datetime | None = None) -> dict | None:
    """The latest touch (or reply) that has no outcome logged after it, if it was recent."""
    now = now or datetime.now(UTC)
    for a in activity:  # newest first
        if a["type"] == ActivityType.OUTCOME.value:
            return None
        if a["type"] in ("call_attempt", "text_sent", "email_sent", "lead_replied"):
            return a if (now - a["occurred_at"]) < timedelta(days=3) else None
    return None


def describe_next(tenant: dict, lead: dict, *, now: datetime | None = None) -> dict[str, Any] | None:
    """What the list and the lead page print: label, when, overdue?"""
    if not lead.get("next_step") or not lead.get("next_step_at"):
        return None
    now = now or datetime.now(UTC)
    at = lead["next_step_at"]
    return {"step": lead["next_step"], "label": step_label(tenant, lead["next_step"]), "when": pretty_when(tenant, at, now=now), "at": at, "overdue": at <= now, "note": lead.get("next_step_note")}

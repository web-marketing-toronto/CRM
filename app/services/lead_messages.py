"""Messages to leads, as opposed to reminders to clients.

Three transactional messages, all sent from the client's own texting number (or with the
client's reply-to address on email) so the conversation lands with the client, not the agency:

    ack          "Thanks Mike, this is Dan at North Ridge Roofing..." within a minute of a form
    decline      the job address is clearly outside the service area; sent after a 15-minute hold
                 the client can cancel from the alert, then the lead is marked Lost (out of area)
    appointment  the client set an estimate date on the lead

plus texts the client types in the app (`send_text`), which go out through Twilio so the thread
in the app stays complete. Imported leads and calls never get any of these. STOP is honoured by
`messaging.send_sms` before anything leaves.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg

from app.config import get_settings
from app.models import ActivityType, AreaStatus, Channel, LeadStatus, LostReason
from app.services import followup, messaging
from app.services import leads as lead_svc
from app.services.normalize import format_phone_display

log = logging.getLogger("crm.lead_messages")

DECLINE_HOLD = timedelta(minutes=15)
ACK_WINDOW = timedelta(hours=24)  # after this an acknowledgement would be odd

TEMPLATES = {
    "ack": "Thanks {first_name}, this is {assignee_first_name} at {client_name}. Got {request}, I'll call you shortly. Reply STOP to opt out.",
    "decline": "Thanks for reaching out. Unfortunately {job_address} is outside the area we serve, so we can't help with this one. Sorry to miss it.",
    "appointment": "Hi {first_name}, {assignee_first_name} from {client_name} will be there {appointment_day} at {appointment_time} to look at {job}. If you need to reschedule, {reschedule}.",
    # What the Text button pre-fills the first time someone texts a lead. Not sent automatically.
    "intro": "Hi {first_name}, thanks for reaching out about {job}. When would be a convenient time to connect and go over what you need? Let me know and we'll get something set up right away. Thanks, {your_name}",
}

DEFAULTS: dict[str, Any] = {
    "ack": True,
    "ack_meta": False,       # Meta instant-form leads only when the form asked for consent to text
    "auto_decline": True,
    "appointment": True,
    "reply_to": "",          # where lead emails reply to; blank = the first owner's address
    "templates": {},
}

PLACEHOLDERS = {
    "first_name": "the lead's first name",
    "name": "their full name",
    "client_name": "the client's business name",
    "assignee_first_name": "first name of whoever the lead is assigned to (or the owner)",
    "your_name": "in the Text button's pre-filled message, the full name of whoever is texting; in automatic messages, the assignee's",
    "your_first_name": "same, first name only",
    "job_type": "the work type, if known",
    "town": "the town from the job address, if known",
    "job_address": "the job address, or 'your location' when there isn't one",
    "request": "'your request for a roof replacement in Bracebridge', shortened to whatever is known",
    "job": "'your roof replacement' or 'the job'",
    "appointment_day": "'Tuesday, Oct 21' (or 'tomorrow' / 'today')",
    "appointment_time": "'2:30 PM'",
    "reschedule": "'reply to this text or call 705 555 0100' depending on what the client has",
}

LEAD_RULES = ("ack", "decline", "appointment")


def settings_for(tenant: dict) -> dict[str, Any]:
    merged = dict(DEFAULTS)
    merged.update((tenant.get("settings") or {}).get("lead_messaging") or {})
    merged["templates"] = {**TEMPLATES, **{k: v for k, v in (merged.get("templates") or {}).items() if v}}
    return merged


def can_reach(tenant: dict, lead: dict) -> bool:
    """Is there any way to message this lead: a phone plus a client texting number, or an email."""
    if lead.get("phone_e164") and tenant.get("twilio_number") and not lead.get("sms_opt_out"):
        return True
    return bool(lead.get("email"))


def _first(name: str | None) -> str:
    return (name or "").strip().split(" ")[0] if name else ""


def _town(address: str | None) -> str:
    if not address:
        return ""
    parts = [p.strip() for p in address.split(",") if p.strip()]
    if len(parts) >= 2:
        return re.sub(r"\s+[A-Z]{2}\b.*$", "", parts[1]).strip() or parts[1]
    return ""


def _assignee_name(conn: psycopg.Connection, tenant: dict, lead: dict) -> str:
    if lead.get("assigned_to"):
        row = conn.execute("SELECT name FROM app_user WHERE id = %s", (lead["assigned_to"],)).fetchone()
        if row:
            return row["name"]
    row = conn.execute("SELECT name FROM app_user WHERE tenant_id = %s AND role = 'owner' AND is_active ORDER BY created_at LIMIT 1", (tenant["id"],)).fetchone()
    return row["name"] if row else tenant["name"]


def _assignee_first(conn: psycopg.Connection, tenant: dict, lead: dict) -> str:
    return _first(_assignee_name(conn, tenant, lead))


def _reply_to(conn: psycopg.Connection, tenant: dict) -> str | None:
    cfg = settings_for(tenant)
    if cfg.get("reply_to"):
        return cfg["reply_to"]
    row = conn.execute("SELECT email FROM app_user WHERE tenant_id = %s AND role = 'owner' AND is_active ORDER BY created_at LIMIT 1", (tenant["id"],)).fetchone()
    return row["email"] if row else None


def context(conn: psycopg.Connection, tenant: dict, lead: dict, *, user: dict | None = None) -> dict[str, str]:
    """Placeholder values for one lead. `user` is whoever is writing (the Text button); automatic messages
    speak as the assignee."""
    tz = followup.tz_for(tenant)
    your_name = (user or {}).get("name") or _assignee_name(conn, tenant, lead)
    job_type = (lead.get("job_type") or "").strip()
    town = _town(lead.get("job_address"))
    if job_type and town:
        request = f"your request for a {job_type.lower()} in {town}"
    elif job_type:
        request = f"your request for a {job_type.lower()}"
    elif town:
        request = f"your request in {town}"
    else:
        request = "your request"
    appt = lead.get("appointment_at")
    day = time_ = ""
    if appt:
        local = appt.astimezone(tz)
        today = datetime.now(UTC).astimezone(tz).date()
        delta = (local.date() - today).days
        day = "today" if delta == 0 else "tomorrow" if delta == 1 else local.strftime("%A, %b %-d")
        time_ = local.strftime("%-I:%M %p")
    phone = tenant.get("twilio_number")
    reschedule = f"reply to this text or call {format_phone_display(phone)}" if phone else "just reply to this text"
    return {
        "first_name": _first(lead.get("contact_name")),
        "name": (lead.get("contact_name") or "").strip(),
        "client_name": tenant["name"],
        "assignee_first_name": _assignee_first(conn, tenant, lead),
        "your_name": your_name,
        "your_first_name": _first(your_name),
        "job_type": job_type,
        "town": town,
        "job_address": lead.get("job_address") or "your location",
        "request": request,
        "job": f"your {job_type.lower()}" if job_type else "the job",
        "appointment_day": day,
        "appointment_time": time_,
        "reschedule": reschedule,
    }


class _Blank(dict):
    def __missing__(self, key: str) -> str:
        return ""


def render(template: str, ctx: dict[str, str]) -> str:
    text = template.format_map(_Blank(ctx))
    text = re.sub(r"\s+([,.!?])", r"\1", text)  # "Thanks , this" -> "Thanks, this"
    text = re.sub(r" {2,}", " ", text)
    return text.strip()


def intro_text(conn: psycopg.Connection, tenant: dict, lead: dict, user: dict) -> str:
    """The pre-filled first text for the Text button, in the client's wording, signed by whoever is texting."""
    template = settings_for(tenant)["templates"]["intro"]
    try:
        return render(template, context(conn, tenant, lead, user=user))
    except (KeyError, ValueError, IndexError):
        return render(TEMPLATES["intro"], context(conn, tenant, lead, user=user))


# --- Scheduling (called from followup.on_lead_created) ---------------------------------------

def plan_for_new_lead(conn: psycopg.Connection, tenant: dict, lead: dict, *, now: datetime) -> None:
    """Decide between an acknowledgement, a held automatic decline, or nothing."""
    cfg = settings_for(tenant)
    lead = conn.execute("SELECT * FROM lead WHERE id = %s", (lead["id"],)).fetchone() or lead
    channel = lead.get("channel")
    flags = lead.get("flags") or {}
    if channel in (Channel.CALL.value, Channel.MANUAL.value, Channel.IMPORT.value) or flags.get("test"):
        return
    if channel == Channel.META_LEAD_AD.value and not cfg["ack_meta"]:
        return
    area = lead.get("area_status")
    precise_out = area == AreaStatus.OUT.value and not flags.get("possibly_out_of_area")
    if precise_out and channel == Channel.WEBSITE_FORM.value and cfg["auto_decline"] and can_reach(tenant, lead):
        due = now + DECLINE_HOLD
        followup.schedule(conn, tenant_id=tenant["id"], lead_id=lead["id"], user_id=None, rule="decline", due_at=due, key=f"{lead['id']}:decline:{followup._ts(now)}")
        conn.execute("UPDATE lead SET flags = flags || %s WHERE id = %s", (psycopg.types.json.Jsonb({"auto_decline_at": due.isoformat()}), lead["id"]))
        return
    if area == AreaStatus.OUT.value or flags.get("possibly_out_of_area"):
        return  # the client decides with one tap from the alert or the lead page
    if cfg["ack"] and can_reach(tenant, lead):
        followup.schedule(conn, tenant_id=tenant["id"], lead_id=lead["id"], user_id=None, rule="ack", due_at=now, key=f"{lead['id']}:ack:{followup._ts(now)}")


# --- Sending -----------------------------------------------------------------------------------

def send_to_lead(conn: psycopg.Connection, tenant: dict, lead: dict, *, kind: str, text: str, reminder_id: UUID | None = None, actor_id: UUID | None = None, actor_label: str = "system", subject: str | None = None) -> messaging.SendResult:
    """Text first (from the client's number), email if there is no number or the text fails."""
    result = messaging.SendResult(False, "none", error="lead has no phone or email")
    number = tenant.get("twilio_number")
    if lead.get("phone_e164") and number and not lead.get("sms_opt_out"):
        result = messaging.send_sms(conn, to=lead["phone_e164"], body=text, kind=kind, tenant_id=tenant["id"], lead_id=lead["id"], user_id=actor_id, reminder_id=reminder_id, from_=number)
    elif lead.get("phone_e164") and not number:
        result = messaging.SendResult(False, "none", error="client has no texting number")
    if not result.ok and lead.get("email"):
        result = messaging.send_email(
            conn, to=lead["email"], subject=subject or f"Your request to {tenant['name']}", text=text, html=None, kind=kind,
            tenant_id=tenant["id"], lead_id=lead["id"], user_id=actor_id, reminder_id=reminder_id, reply_to=_reply_to(conn, tenant), from_name=tenant["name"],
        )
    detail = {"kind": kind, "channel": result.provider if result.ok else None, "delivered": result.ok, "error": result.error}
    lead_svc.log_activity(conn, tenant["id"], lead["id"], ActivityType.AUTO_MESSAGE if actor_id is None else ActivityType.TEXT_SENT, actor_id=actor_id, actor_label=actor_label, body=text, detail=detail)
    return result


def dispatch(conn: psycopg.Connection, reminder: dict, tenant: dict, lead: dict, *, now: datetime) -> None:
    """Called by followup.dispatch_due for the lead-facing rules."""
    rule = reminder["rule"]
    cfg = settings_for(tenant)
    ctx = context(conn, tenant, lead)
    if rule == "ack":
        if lead["status"] != LeadStatus.NEW.value and lead.get("last_touch_at") and (now - lead["created_at"]) > timedelta(minutes=5):
            return  # the client already reached them; an automated "I'll call you shortly" would be odd
        if now - lead["created_at"] > ACK_WINDOW:
            return
        send_to_lead(conn, tenant, lead, kind="ack", text=render(cfg["templates"]["ack"], ctx), reminder_id=reminder["id"])
    elif rule == "decline":
        if not (lead.get("flags") or {}).get("auto_decline_at"):
            return  # cancelled from the alert
        send_to_lead(conn, tenant, lead, kind="decline", text=render(cfg["templates"]["decline"], ctx), reminder_id=reminder["id"], subject=f"Your request to {tenant['name']}")
        conn.execute("UPDATE lead SET flags = flags - 'auto_decline_at' WHERE id = %s", (lead["id"],))
        fresh = conn.execute("SELECT * FROM lead WHERE id = %s", (lead["id"],)).fetchone()
        if fresh["status"] not in (LeadStatus.WON.value, LeadStatus.LOST.value):
            lead_svc.change_status(conn, fresh, LeadStatus.LOST, actor_id=None, actor_label="system", lost_reason=LostReason.OUT_OF_AREA, note="Automatic: job address outside the service area", now=now)
    elif rule == "appointment":
        if not lead.get("appointment_at") or not cfg["appointment"]:
            return
        send_to_lead(conn, tenant, lead, kind="appointment", text=render(cfg["templates"]["appointment"], ctx), reminder_id=reminder["id"], subject=f"Your appointment with {tenant['name']}")


# --- Actions from the app and action links -----------------------------------------------------

def cancel_auto_decline(conn: psycopg.Connection, tenant: dict, lead: dict, *, actor_id: UUID | None, actor_label: str, now: datetime | None = None) -> str:
    """'We do serve this address': stop the held decline, clear the flags, send the acknowledgement instead."""
    now = now or datetime.now(UTC)
    followup.cancel(conn, lead["id"], ("decline",))
    conn.execute("UPDATE lead SET flags = (flags - 'auto_decline_at') - 'possibly_out_of_area', area_status = 'in' WHERE id = %s", (lead["id"],))
    lead_svc.log_activity(conn, tenant["id"], lead["id"], ActivityType.NOTE, actor_id=actor_id, actor_label=actor_label, body="Confirmed the address is in the service area")
    cfg = settings_for(tenant)
    if cfg["ack"] and can_reach(tenant, lead) and lead["status"] == LeadStatus.NEW.value and (now - lead["created_at"]) <= ACK_WINDOW and lead.get("channel") not in (Channel.CALL.value, Channel.MANUAL.value, Channel.IMPORT.value):
        followup.schedule(conn, tenant_id=tenant["id"], lead_id=lead["id"], user_id=None, rule="ack", due_at=now, key=f"{lead['id']}:ack:{followup._ts(now)}")
        return "Kept the lead; sending them the acknowledgement now."
    return "Kept the lead."


def decline_now(conn: psycopg.Connection, tenant: dict, lead: dict, *, actor_id: UUID | None, actor_label: str, send: bool = True, now: datetime | None = None) -> str:
    """One tap from the alert or the lead page: tell the lead we don't serve the area and mark it Lost."""
    now = now or datetime.now(UTC)
    followup.cancel(conn, lead["id"], ("decline", "ack"))
    conn.execute("UPDATE lead SET flags = flags - 'auto_decline_at' WHERE id = %s", (lead["id"],))
    sent = None
    if send and can_reach(tenant, lead):
        cfg = settings_for(tenant)
        sent = send_to_lead(conn, tenant, lead, kind="decline", text=render(cfg["templates"]["decline"], context(conn, tenant, lead)), subject=f"Your request to {tenant['name']}")
    if lead["status"] not in (LeadStatus.WON.value, LeadStatus.LOST.value):
        lead_svc.change_status(conn, lead, LeadStatus.LOST, actor_id=actor_id, actor_label=actor_label, lost_reason=LostReason.OUT_OF_AREA, now=now)
    if sent is None:
        return "Marked Lost: out of area."
    return "Marked Lost and let them know." if sent.ok else f"Marked Lost; the message didn't go out ({sent.error})."


def set_appointment(conn: psycopg.Connection, tenant: dict, lead: dict, when: datetime | None, *, actor_id: UUID | None, actor_label: str, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    tz = followup.tz_for(tenant)
    if when is None:
        conn.execute("UPDATE lead SET appointment_at = NULL WHERE id = %s", (lead["id"],))
        followup.cancel(conn, lead["id"], ("appointment", "after_appointment"))
        lead_svc.log_activity(conn, tenant["id"], lead["id"], ActivityType.APPOINTMENT, actor_id=actor_id, actor_label=actor_label, body="Appointment cleared")
        return "Appointment cleared."
    conn.execute("UPDATE lead SET appointment_at = %s WHERE id = %s", (when, lead["id"]))
    pretty = when.astimezone(tz).strftime("%a %b %-d, %-I:%M %p")
    lead_svc.log_activity(conn, tenant["id"], lead["id"], ActivityType.APPOINTMENT, actor_id=actor_id, actor_label=actor_label, body=f"Appointment set for {pretty}", detail={"at": when.isoformat()})
    # Setting a date is contact; pause the cadence until after the visit and ask how it went.
    if lead["status"] == LeadStatus.NEW.value:
        lead_svc.change_status(conn, lead, LeadStatus.CONTACTED, actor_id=actor_id, actor_label=actor_label, now=now)
    followup.cancel(conn, lead["id"], ("nudge", "cadence", "reply_nudge", "snoozed", "appointment", "after_appointment", "escalation"))
    followup.acknowledge(conn, lead["id"])
    followup.schedule(conn, tenant_id=tenant["id"], lead_id=lead["id"], user_id=lead.get("assigned_to"), rule="after_appointment", due_at=followup.next_business_time(when + timedelta(hours=3), tenant), key=f"{lead['id']}:after_appointment:{followup._ts(when)}")
    followup._schedule_cadence(conn, tenant, lead, when + timedelta(hours=3))
    cfg = settings_for(tenant)
    if cfg["appointment"] and can_reach(tenant, lead) and lead.get("channel") != Channel.IMPORT.value and when > now:
        followup.schedule(conn, tenant_id=tenant["id"], lead_id=lead["id"], user_id=None, rule="appointment", due_at=now, key=f"{lead['id']}:appointment:{followup._ts(when)}")
        return f"Appointment set for {pretty}; confirmation on its way to them."
    return f"Appointment set for {pretty}."


def connect_token(lead_id: UUID, user_id: UUID) -> str:
    from app.auth import crypto

    return crypto.signed_token({"l": str(lead_id), "u": str(user_id)}, salt="connect")


def start_call(conn: psycopg.Connection, tenant: dict, lead: dict, user: dict, *, actor_label: str) -> messaging.SendResult:
    """Click-to-call: ring the user's mobile from the client's number, then connect them to the lead with the
    client's number as caller id. Logged as a call attempt."""
    if not lead.get("phone_e164"):
        raise lead_svc.LeadError("This lead has no phone number")
    if not tenant.get("twilio_number"):
        raise lead_svc.LeadError(f"Calling through the CRM needs a texting number for {tenant['name']}; ask {get_settings().agency_name}")
    if not user.get("mobile_e164"):
        raise lead_svc.LeadError("Add your mobile number under Account first; that's the phone we ring")
    url = f"{get_settings().base_url}/v1/twilio/connect/{connect_token(lead['id'], user['id'])}"
    result = messaging.start_call(to=user["mobile_e164"], from_=tenant["twilio_number"], connect_url=url)
    if not result.ok:
        raise lead_svc.LeadError(f"The call didn't start: {result.error}")
    lead_svc.log_touch(conn, lead, ActivityType.CALL_ATTEMPT, actor_id=user["id"], actor_label=actor_label, note=f"Called through the CRM from {format_phone_display(tenant['twilio_number'])}", detail={"kind": "crm_call", "provider_id": result.provider_id})
    return result


def send_text(conn: psycopg.Connection, tenant: dict, lead: dict, body: str, *, actor_id: UUID, actor_label: str, via: str = "app") -> messaging.SendResult:
    """A text typed in the app (or relayed from the client's phone). Goes out from the client's number and counts as a touch."""
    body = body.strip()
    if not body:
        raise lead_svc.LeadError("Type the message first")
    if not lead.get("phone_e164"):
        raise lead_svc.LeadError("This lead has no phone number")
    if lead.get("sms_opt_out"):
        raise lead_svc.LeadError("They texted STOP; the CRM won't text this number again")
    number = tenant.get("twilio_number")
    if not number:
        raise lead_svc.LeadError(f"Texting from the app needs a texting number for {tenant['name']}; ask {get_settings().agency_name}")
    result = messaging.send_sms(conn, to=lead["phone_e164"], body=body, kind="lead_text", tenant_id=tenant["id"], lead_id=lead["id"], user_id=actor_id, from_=number)
    if not result.ok:
        raise lead_svc.LeadError(f"The text didn't send: {result.error}")
    lead_svc.log_touch(conn, lead, ActivityType.TEXT_SENT, actor_id=actor_id, actor_label=actor_label, note=body, detail={"kind": "lead_text", "via": via, "provider_id": result.provider_id})
    return result

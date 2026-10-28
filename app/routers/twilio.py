"""Twilio webhooks: incoming texts and delivery receipts.

    POST /v1/twilio/sms      a text arrived at one of our numbers
    POST /v1/twilio/status   delivery status for a text we sent

Every request is checked against Twilio's X-Twilio-Signature (HMAC-SHA1 of the URL plus the
sorted form fields, keyed with the account's auth token). Replies to the sender go back as
TwiML in the response, so a client who texts "1" gets "Logged: called ..." straight back.

Who is texting us?
    * a client's registered mobile  -> a reply code (1/2/3 logged a touch, 4/5 snooze, W/L/K/N)
    * the phone on an existing lead -> the lead replied: alert the client, restart the cadence
    * STOP from a lead              -> opt-out; we never text that number for that client again
    * an unknown number, texting a client's number -> a brand-new lead (channel: text message)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response

from app.config import get_settings
from app.db import system_db
from app.models import ActivityType, Channel, Platform
from app.services import followup, lead_messages, messaging, worker
from app.services import leads as lead_svc
from app.services.attribution import IntakeFields
from app.services.normalize import format_phone_display, normalize_phone

log = logging.getLogger("crm.twilio")
router = APIRouter(prefix="/v1/twilio", tags=["twilio"])

STOP_WORDS = {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"}
START_WORDS = {"START", "UNSTOP", "YES"}


def twilio_signature(url: str, params: dict[str, str], token: str) -> str:
    data = url + "".join(k + v for k, v in sorted(params.items()))
    return base64.b64encode(hmac.new(token.encode(), data.encode("utf-8"), hashlib.sha1).digest()).decode()


def _candidate_urls(request: Request) -> list[str]:
    s = get_settings()
    path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    urls = [f"{s.base_url.rstrip('/')}{path}", str(request.url)]
    # Behind a proxy the scheme Twilio used is https even if we see http.
    urls.append(str(request.url).replace("http://", "https://", 1))
    return list(dict.fromkeys(urls))


async def _verified_form(request: Request) -> dict[str, str]:
    token = get_settings().twilio_auth_token
    if not token:
        raise HTTPException(status_code=503, detail="Twilio is not configured")
    form = await request.form()
    params = {k: str(v) for k, v in form.items() if isinstance(v, str)}
    sent = request.headers.get("x-twilio-signature", "")
    if not sent or not any(hmac.compare_digest(twilio_signature(u, params, token), sent) for u in _candidate_urls(request)):
        log.warning("twilio webhook with a bad signature from %s", request.client.host if request.client else "?")
        raise HTTPException(status_code=403, detail="bad signature")
    return params


def _twiml(message: str | None = None) -> Response:
    if message:
        safe = message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        body = f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{safe}</Message></Response>'
    else:
        body = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
    return Response(content=body, media_type="application/xml")


@router.post("/sms")
async def inbound_sms(request: Request, background: BackgroundTasks):
    params = await _verified_form(request)
    from_ = normalize_phone(params.get("From", "")) or params.get("From", "")
    to = normalize_phone(params.get("To", "")) or params.get("To", "")
    body = (params.get("Body") or "").strip()
    sid = params.get("MessageSid")
    settings = get_settings()

    with system_db() as conn:
        tenant = conn.execute("SELECT * FROM tenant WHERE twilio_number = %s AND is_active", (to,)).fetchone() if to else None
        is_agency_number = bool(settings.twilio_agency_from) and to == normalize_phone(settings.twilio_agency_from)

        # 1. A client (or agency admin) texting a reply code from their registered mobile.
        users = conn.execute("SELECT * FROM app_user WHERE mobile_e164 = %s AND is_active", (from_,)).fetchall()
        if tenant is not None:
            scoped = [u for u in users if u["tenant_id"] == tenant["id"]]
            users = scoped or users
        if users:
            user = _most_recently_reminded(conn, users)
            # Free text to the client's own number is a message for a lead, relayed on from that number.
            if tenant is not None and followup.relay_enabled(tenant) and user["tenant_id"] == tenant["id"] and not followup.looks_like_code(body):
                lead, message = followup.relay_target(conn, user, tenant, body)
                if lead is None:
                    messaging.record_inbound_sms(conn, tenant_id=tenant["id"], lead_id=None, user_id=user["id"], from_=from_, to=to, body=body, provider_id=sid)
                    return _twiml("Which lead is that for? Start with their ref, e.g. A14 Yes, Tuesday works.")
                messaging.record_inbound_sms(conn, tenant_id=tenant["id"], lead_id=lead["id"], user_id=user["id"], from_=from_, to=to, body=body, provider_id=sid)
                try:
                    lead_messages.send_text(conn, tenant, lead, message, actor_id=user["id"], actor_label=user["name"], via="phone")
                except lead_svc.LeadError as exc:
                    return _twiml(f"Not sent to {lead.get('contact_name') or lead['ref_code']}: {exc}")
                return _twiml()  # no confirmation text: the thread on their phone already shows it
            messaging.record_inbound_sms(conn, tenant_id=user["tenant_id"], lead_id=None, user_id=user["id"], from_=from_, to=to, body=body, provider_id=sid)
            outcome = followup.apply_text_reply(conn, user, body)
            if outcome.lead_id and outcome.handled:
                background.add_task(worker.dispatch_lead_now, outcome.lead_id)
            return _twiml(outcome.reply)

        if tenant is None:
            if is_agency_number:
                messaging.record_inbound_sms(conn, tenant_id=None, lead_id=None, user_id=None, from_=from_, to=to, body=body, provider_id=sid)
                return _twiml(f"This number sends lead alerts for {settings.agency_name}. Open the link in an alert to update a lead.")
            log.warning("text to an unknown number %s from %s", to, from_)
            return _twiml()

        # 2. STOP / START from anyone texting a client's number.
        word = re.sub(r"[^A-Z]", "", body.upper().split()[0]) if body else ""
        lead = conn.execute(
            "SELECT * FROM lead WHERE tenant_id = %s AND phone_e164 = %s ORDER BY created_at DESC LIMIT 1", (tenant["id"], from_)
        ).fetchone()
        if word in STOP_WORDS:
            conn.execute("INSERT INTO sms_opt_out (tenant_id, phone_e164) VALUES (%s, %s) ON CONFLICT DO NOTHING", (tenant["id"], from_))
            if lead is not None:
                conn.execute("UPDATE lead SET sms_opt_out = true WHERE tenant_id = %s AND phone_e164 = %s", (tenant["id"], from_))
                lead_svc.log_activity(conn, tenant["id"], lead["id"], ActivityType.NOTE, actor_label=lead.get("contact_name") or "lead", body="Texted STOP: no more automatic texts to this number")
            messaging.record_inbound_sms(conn, tenant_id=tenant["id"], lead_id=lead["id"] if lead else None, user_id=None, from_=from_, to=to, body=body, provider_id=sid)
            return _twiml()  # Twilio sends its own opt-out confirmation
        if word in START_WORDS and lead is not None and lead["sms_opt_out"]:
            conn.execute("DELETE FROM sms_opt_out WHERE tenant_id = %s AND phone_e164 = %s", (tenant["id"], from_))
            conn.execute("UPDATE lead SET sms_opt_out = false WHERE tenant_id = %s AND phone_e164 = %s", (tenant["id"], from_))
            messaging.record_inbound_sms(conn, tenant_id=tenant["id"], lead_id=lead["id"], user_id=None, from_=from_, to=to, body=body, provider_id=sid)
            return _twiml()

        # 3. A lead replying.
        if lead is not None:
            messaging.record_inbound_sms(conn, tenant_id=tenant["id"], lead_id=lead["id"], user_id=None, from_=from_, to=to, body=body, provider_id=sid)
            followup.on_lead_replied(conn, tenant, lead, body or "(empty message)")
            background.add_task(worker.dispatch_lead_now, lead["id"])
            return _twiml()

        # 4. Someone new.
        fields = IntakeFields(phone_raw=from_, message=body or None, channel=Channel.INBOUND_TEXT, platform=Platform.UNKNOWN)
        try:
            result = lead_svc.create_from_intake(conn, tenant, fields, raw_payload={"From": from_, "To": to, "Body": body, "MessageSid": sid}, ip=None, user_agent="twilio", maps_api_key=settings.google_maps_api_key)
        except lead_svc.LeadError as exc:
            log.warning("inbound text could not become a lead: %s", exc)
            return _twiml()
        messaging.record_inbound_sms(conn, tenant_id=tenant["id"], lead_id=result.lead_id, user_id=None, from_=from_, to=to, body=body, provider_id=sid)
        background.add_task(worker.dispatch_lead_now, result.lead_id)
        return _twiml()


def _most_recently_reminded(conn, users: list[dict]) -> dict:
    if len(users) == 1:
        return users[0]
    ids = [u["id"] for u in users]
    row = conn.execute(
        "SELECT user_id FROM reminder WHERE user_id = ANY(%s) AND sent_at IS NOT NULL ORDER BY sent_at DESC LIMIT 1", (ids,)
    ).fetchone()
    if row:
        for u in users:
            if u["id"] == row["user_id"]:
                return u
    return users[0]


def _xml(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


RING_MODES = ("both", "office_first", "mobile_first")
OFFICE_RING_SECONDS = 18   # long enough for a receptionist, short enough that a lead doesn't give up
MOBILE_RING_SECONDS = 25


def forward_targets(conn, tenant: dict, *, now: datetime | None = None) -> tuple[list[str], str]:
    """Where calls to the client's texting number ring: (numbers in order, ring mode).
    The office line only rings inside business hours when that option is on; the mobile is the explicit
    forwarding number, else the first owner's mobile."""
    cfg = (tenant.get("settings") or {}).get("sms") or {}
    mobile = cfg.get("forward_calls_to") or ""
    if not mobile:
        row = conn.execute("SELECT mobile_e164 FROM app_user WHERE tenant_id = %s AND role = 'owner' AND is_active AND mobile_e164 IS NOT NULL ORDER BY created_at LIMIT 1", (tenant["id"],)).fetchone()
        mobile = row["mobile_e164"] if row else ""
    office = cfg.get("office_line") or ""
    if office and cfg.get("office_hours_only", True) and not followup.in_business_hours(now or datetime.now(UTC), tenant):
        office = ""
    mode = cfg.get("ring") if cfg.get("ring") in RING_MODES else "both"
    order = [mobile, office] if mode == "mobile_first" else [office, mobile]
    targets = [n for n in dict.fromkeys(order) if n]
    return targets, mode


def forward_number(conn, tenant: dict) -> str | None:
    """Backwards-compatible single target: the first number that would ring."""
    targets, _ = forward_targets(conn, tenant)
    return targets[0] if targets else None


def _dial(from_: str, numbers: list[str], *, timeout: int, action: str | None = None) -> str:
    inner = "".join(f"<Number>{_xml(n)}</Number>" for n in numbers)
    act = f' action="{_xml(action)}" method="POST"' if action else ""
    return f'<Dial callerId="{_xml(from_)}" timeout="{timeout}"{act}>{inner}</Dial>'


def _missed(tenant_name: str) -> str:
    return f'<Say voice="alice">Sorry, we missed you. Please text this number and {_xml(tenant_name)} will get right back to you.</Say>'


def _ring_plan(from_: str, targets: list[str], mode: str, tenant_name: str) -> str:
    """TwiML for the first leg. Simultaneous: one Dial with every number. In turn: dial the first with an
    action URL that decides whether to try the next."""
    s = get_settings()
    if mode == "both" or len(targets) == 1:
        return _dial(from_, targets, timeout=MOBILE_RING_SECONDS) + _missed(tenant_name)
    from app.auth import crypto

    token = crypto.signed_token({"f": from_, "t": targets[1:], "n": tenant_name}, salt="voicenext")
    first_timeout = OFFICE_RING_SECONDS if mode == "office_first" else MOBILE_RING_SECONDS
    return _dial(from_, targets[:1], timeout=first_timeout, action=f"{s.base_url}/v1/twilio/voice/next/{token}")


@router.post("/voice")
async def inbound_call(request: Request):
    """A lead calls the client's texting number back: ring the office and/or the mobile, showing the lead's number."""
    params = await _verified_form(request)
    from_ = normalize_phone(params.get("From", "")) or params.get("From", "")
    to = normalize_phone(params.get("To", "")) or params.get("To", "")
    with system_db() as conn:
        tenant = conn.execute("SELECT * FROM tenant WHERE twilio_number = %s AND is_active", (to,)).fetchone() if to else None
        targets, mode = forward_targets(conn, tenant) if tenant else ([], "both")
        if tenant is None or not targets:
            body = '<?xml version="1.0" encoding="UTF-8"?><Response><Say voice="alice">Sorry, this number cannot take calls right now. Please text us instead.</Say></Response>'
            return Response(content=body, media_type="application/xml")
        lead = conn.execute("SELECT * FROM lead WHERE tenant_id = %s AND phone_e164 = %s ORDER BY created_at DESC LIMIT 1", (tenant["id"], from_)).fetchone()
        if lead is not None:
            where = " and ".join(format_phone_display(n) for n in targets)
            followup.on_lead_replied(conn, tenant, lead, f"Called the texting number; forwarded to {where}", alert=False)
    body = '<?xml version="1.0" encoding="UTF-8"?><Response>' + _ring_plan(from_, targets, mode, tenant["name"]) + "</Response>"
    return Response(content=body, media_type="application/xml")


@router.post("/voice/next/{token}")
async def inbound_call_next(request: Request, token: str):
    """After a sequential Dial: if nobody answered, ring the next number; if it was answered and has ended,
    hang up rather than ringing someone else."""
    from app.auth import crypto

    params = await _verified_form(request)
    data = crypto.read_signed_token(token, salt="voicenext", max_age_seconds=600)
    if not data:
        return Response(content='<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>', media_type="application/xml")
    status = (params.get("DialCallStatus") or "").lower()
    if status in ("completed", "answered"):
        return Response(content='<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>', media_type="application/xml")
    remaining = list(data.get("t") or [])
    if not remaining:
        body = '<?xml version="1.0" encoding="UTF-8"?><Response>' + _missed(data.get("n", "")) + "</Response>"
        return Response(content=body, media_type="application/xml")
    nxt = crypto.signed_token({"f": data["f"], "t": remaining[1:], "n": data.get("n", "")}, salt="voicenext")
    body = (
        '<?xml version="1.0" encoding="UTF-8"?><Response>'
        + _dial(data["f"], remaining[:1], timeout=MOBILE_RING_SECONDS, action=f"{get_settings().base_url}/v1/twilio/voice/next/{nxt}")
        + "</Response>"
    )
    return Response(content=body, media_type="application/xml")


@router.post("/connect/{token}")
async def connect_call(request: Request, token: str):
    """Second leg of click-to-call: the client answered; now dial the lead showing the client's business number."""
    from app.auth import crypto

    await _verified_form(request)
    data = crypto.read_signed_token(token, salt="connect", max_age_seconds=600)
    if not data:
        return Response(content='<?xml version="1.0" encoding="UTF-8"?><Response><Say voice="alice">Sorry, this call link has expired.</Say></Response>', media_type="application/xml")
    with system_db() as conn:
        lead = conn.execute("SELECT l.*, t.twilio_number, t.name AS tenant_name FROM lead l JOIN tenant t ON t.id = l.tenant_id WHERE l.id = %s", (data["l"],)).fetchone()
    if lead is None or not lead["phone_e164"] or not lead["twilio_number"]:
        return Response(content='<?xml version="1.0" encoding="UTF-8"?><Response><Say voice="alice">Sorry, that lead has no phone number.</Say></Response>', media_type="application/xml")
    name = _xml(lead.get("contact_name") or "the lead")
    body = (
        '<?xml version="1.0" encoding="UTF-8"?><Response>'
        f'<Say voice="alice">Connecting you to {name}.</Say>'
        f'<Dial callerId="{_xml(lead["twilio_number"])}" timeout="30">{_xml(lead["phone_e164"])}</Dial>'
        "</Response>"
    )
    return Response(content=body, media_type="application/xml")


@router.post("/status")
async def delivery_status(request: Request):
    params = await _verified_form(request)
    sid = params.get("MessageSid") or params.get("SmsSid")
    status = (params.get("MessageStatus") or params.get("SmsStatus") or "").lower()
    error = params.get("ErrorCode")
    if sid and status:
        with system_db() as conn:
            messaging.update_delivery(conn, sid, status, f"Twilio error {error}" if error else None)
    return Response(status_code=204)

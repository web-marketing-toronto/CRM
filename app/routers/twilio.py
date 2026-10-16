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
                    return _twiml("Which lead is that for? Start with their ref, e.g. A14 Yes, Tuesday works. (1-5, W, L, K, N are still the codes.)")
                messaging.record_inbound_sms(conn, tenant_id=tenant["id"], lead_id=lead["id"], user_id=user["id"], from_=from_, to=to, body=body, provider_id=sid)
                try:
                    lead_messages.send_text(conn, tenant, lead, message, actor_id=user["id"], actor_label=user["name"], via="phone")
                except lead_svc.LeadError as exc:
                    return _twiml(f"Not sent to {lead.get('contact_name') or lead['ref_code']}: {exc}")
                return _twiml()  # no confirmation text: the thread on their phone already shows it
            messaging.record_inbound_sms(conn, tenant_id=user["tenant_id"], lead_id=None, user_id=user["id"], from_=from_, to=to, body=body, provider_id=sid)
            outcome = followup.apply_reply_code(conn, user, body)
            if outcome.lead_id and outcome.handled:
                background.add_task(worker.dispatch_lead_now, outcome.lead_id)
            return _twiml(outcome.reply)

        if tenant is None:
            if is_agency_number:
                messaging.record_inbound_sms(conn, tenant_id=None, lead_id=None, user_id=None, from_=from_, to=to, body=body, provider_id=sid)
                return _twiml(f"This number sends lead alerts for {settings.agency_name}. Reply codes only work from the mobile number on your account.")
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


def forward_number(conn, tenant: dict) -> str | None:
    """Where calls to the client's texting number ring: the setting, else the first owner's mobile."""
    cfg = (tenant.get("settings") or {}).get("sms") or {}
    if cfg.get("forward_calls_to"):
        return cfg["forward_calls_to"]
    row = conn.execute("SELECT mobile_e164 FROM app_user WHERE tenant_id = %s AND role = 'owner' AND is_active AND mobile_e164 IS NOT NULL ORDER BY created_at LIMIT 1", (tenant["id"],)).fetchone()
    return row["mobile_e164"] if row else None


@router.post("/voice")
async def inbound_call(request: Request):
    """A lead calls the client's texting number back: ring the client's phone, showing the lead's number."""
    params = await _verified_form(request)
    from_ = normalize_phone(params.get("From", "")) or params.get("From", "")
    to = normalize_phone(params.get("To", "")) or params.get("To", "")
    with system_db() as conn:
        tenant = conn.execute("SELECT * FROM tenant WHERE twilio_number = %s AND is_active", (to,)).fetchone() if to else None
        target = forward_number(conn, tenant) if tenant else None
        if tenant is None or not target:
            body = '<?xml version="1.0" encoding="UTF-8"?><Response><Say voice="alice">Sorry, this number cannot take calls right now. Please text us instead.</Say></Response>'
            return Response(content=body, media_type="application/xml")
        lead = conn.execute("SELECT * FROM lead WHERE tenant_id = %s AND phone_e164 = %s ORDER BY created_at DESC LIMIT 1", (tenant["id"], from_)).fetchone()
        if lead is not None:
            followup.on_lead_replied(conn, tenant, lead, f"Called the texting number; forwarded to {format_phone_display(target)}", alert=False)
    body = (
        '<?xml version="1.0" encoding="UTF-8"?><Response>'
        f'<Dial callerId="{_xml(from_)}" timeout="25">{_xml(target)}</Dial>'
        f'<Say voice="alice">Sorry, we missed you. Please text this number and {_xml(tenant["name"])} will get right back to you.</Say>'
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

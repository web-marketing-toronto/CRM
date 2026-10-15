"""Lead sources that don't speak plain form posts: CallRail, Meta Lead Ads, Google Ads lead forms.

    POST /v1/callrail/{slug}/{key}       CallRail post-call webhook (per client)
    GET  /v1/meta/webhook                Meta's one-time subscription check
    POST /v1/meta/webhook                Meta leadgen notifications (one endpoint; the Page id picks the client)
    POST /v1/google-leads/{slug}/{key}   Google Ads lead form webhook (per client)

Each one turns its payload into the same IntakeFields the form endpoint uses, so dedupe,
service-area checks, alerts and acknowledgements all behave the same whatever the source.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from app.config import get_settings
from app.db import system_db
from app.models import ActivityType, Channel, Platform
from app.services import attribution, followup, worker
from app.services import leads as lead_svc
from app.services.normalize import normalize_phone

log = logging.getLogger("crm.sources")
router = APIRouter(prefix="/v1", tags=["sources"])

GRAPH = "https://graph.facebook.com/v21.0"

CALLRAIL_DEFAULTS = {"min_seconds": 30, "drop_tags": ["spam", "vendor", "solicitor", "existing customer", "wrong number", "not a lead"]}


def _tenant_by_key(conn, slug: str, key: str | None) -> dict:
    tenant = conn.execute("SELECT * FROM tenant WHERE slug = %s AND is_active", (slug,)).fetchone()
    if tenant is None or not key or not hmac.compare_digest(tenant["intake_key"], key):
        raise HTTPException(status_code=404, detail="unknown endpoint")
    return tenant


async def _json_body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if len(raw) > 256 * 1024:
        raise HTTPException(status_code=413, detail="payload too large")
    ctype = (request.headers.get("content-type") or "").lower()
    if "json" in ctype or raw[:1] == b"{":
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail="invalid JSON") from exc
        return data if isinstance(data, dict) else {}
    form = await request.form()
    return {k: v for k, v in form.items() if isinstance(v, str)}


# --- CallRail ----------------------------------------------------------------------------------

def _callrail_platform(data: dict[str, Any]) -> tuple[Platform | None, dict[str, str]]:
    """CallRail's own attribution into our fields. Returns an explicit platform when its source name says so."""
    attr: dict[str, str] = {}
    for ours, theirs in (
        ("gclid", "gclid"), ("utm_source", "utm_source"), ("utm_medium", "utm_medium"), ("utm_campaign", "utm_campaign"),
        ("utm_term", "utm_term"), ("utm_content", "utm_content"), ("landing_page", "landing_page_url"), ("referrer", "referring_url"),
    ):
        if data.get(theirs):
            attr[ours] = str(data[theirs])
    if not attr.get("utm_term") and data.get("keywords"):
        attr["utm_term"] = str(data["keywords"])
    source = str(data.get("source") or data.get("source_name") or "").strip()
    medium = str(data.get("medium") or "").strip().lower()
    if source and not attr.get("utm_source"):
        attr["utm_source"] = source
    if medium and not attr.get("utm_medium"):
        attr["utm_medium"] = medium
    if data.get("campaign") and not attr.get("campaign_name"):
        attr["campaign_name"] = str(data["campaign"])
    s = source.lower()
    platform = None
    if attr.get("gclid") or "google ads" in s or "adwords" in s or ("google" in s and medium in ("cpc", "ppc", "paid")):
        platform = Platform.GOOGLE_ADS
    elif ("facebook" in s or "meta" in s or "instagram" in s) and medium in ("cpc", "ppc", "paid", "paid social", "social-paid", "paid_social"):
        platform = Platform.META_ADS
    elif "organic" in s or medium == "organic" or "google my business" in s or "business profile" in s:
        platform = Platform.ORGANIC
    elif s in ("direct", "direct call", "offline", "print") or medium in ("direct", "offline"):
        platform = Platform.DIRECT
    return platform, attr


def _seconds(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _mmss(seconds: int) -> str:
    return f"{seconds // 60} min {seconds % 60} s" if seconds >= 60 else f"{seconds} s"


@router.post("/callrail/{slug}/{key}")
async def callrail(slug: str, key: str, request: Request, background: BackgroundTasks):
    settings = get_settings()
    raw = await request.body()
    if settings.callrail_signing_key:
        sent = request.headers.get("signature", "")
        want = base64.b64encode(hmac.new(settings.callrail_signing_key.encode(), raw, hashlib.sha1).digest()).decode()
        if not sent or not hmac.compare_digest(sent, want):
            raise HTTPException(status_code=403, detail="bad signature")
    data = await _json_body(request)
    with system_db() as conn:
        tenant = _tenant_by_key(conn, slug, key)
        cfg = {**CALLRAIL_DEFAULTS, **((tenant.get("settings") or {}).get("callrail") or {})}
        direction = str(data.get("direction") or "inbound").lower()
        if direction != "inbound":
            return JSONResponse({"ok": True, "ignored": "outbound call"})
        tags = [str(t).strip().lower() for t in (data.get("tags") or [])] if isinstance(data.get("tags"), list) else [str(data.get("tags") or "").lower()]
        drop = {str(t).lower() for t in cfg["drop_tags"]}
        if any(t in drop for t in tags) or str(data.get("lead_status") or "").lower() in ("not_a_lead", "not a lead"):
            log.info("callrail %s: dropped call %s (tags %s)", slug, data.get("id"), tags)
            return JSONResponse({"ok": True, "ignored": "tagged " + ", ".join(t for t in tags if t in drop)})
        phone = str(data.get("customer_phone_number") or data.get("caller_number") or data.get("customernumber") or "")
        answered = data.get("answered") in (True, "true", "1", 1)
        voicemail = data.get("voicemail") in (True, "true", "1", 1)
        duration = _seconds(data.get("duration"))
        real = answered and not voicemail and duration >= int(cfg["min_seconds"])
        call_id = str(data.get("id") or data.get("call_id") or "")
        platform, attr = _callrail_platform(data)
        summary = f"Answered call, {_mmss(duration)}" if real else ("Voicemail" if voicemail else "Missed call" if not answered else f"Short call, {_mmss(duration)}")
        note = str(data.get("note") or data.get("transcription") or "").strip()
        city = ", ".join(x for x in (data.get("customer_city"), data.get("customer_state")) if x)

        flat: dict[str, Any] = {
            "name": data.get("customer_name") or "", "phone": phone, "channel": "call", "message": note or None,
            "callrail_call_id": call_id, "form_name": data.get("campaign") or data.get("tracking_phone_number") or "",
            **attr,
        }
        if city and not note:
            flat["contact_address"] = city
        fields = attribution.interpret(flat, tenant.get("field_map") or {}, default_channel=Channel.CALL)
        if platform is not None:
            fields.platform = platform
        phone_e164 = normalize_phone(phone)
        if not phone_e164:
            raise HTTPException(status_code=422, detail="no caller number in the payload (customer_phone_number)")

        existing = lead_svc.find_duplicate(conn, tenant["id"], phone_e164, None)
        payload = {**data, "missed": not real, "call_summary": summary}
        if existing is not None and (datetime.now(UTC) - existing["created_at"]) <= lead_svc.DEDUPE_WINDOW:
            lead_svc._insert_source(conn, tenant["id"], existing["id"], fields, fields.attribution, payload, None, "callrail")
            lead = conn.execute("SELECT * FROM lead WHERE id = %s", (existing["id"],)).fetchone()
            if real:
                # They called and someone answered: that is contact, made by the client.
                lead_svc.log_touch(conn, lead, ActivityType.CALL_ATTEMPT, actor_id=None, actor_label="CallRail", note=f"They called in. {summary}." + (f" Note: {note}" if note else ""), detail={"kind": "inbound_call", "callrail_call_id": call_id, "recording": data.get("recording_player") or data.get("recording")})
            else:
                followup.on_lead_replied(conn, tenant, lead, f"{summary} from them. Call back." + (f" {note}" if note else ""))
            background.add_task(worker.dispatch_lead_now, lead["id"])
            return JSONResponse({"ok": True, "lead_id": str(lead["id"]), "ref": lead["ref_code"], "created": False, "call": summary})

        result = lead_svc.create_from_intake(conn, tenant, fields, raw_payload=payload, ip=None, user_agent="callrail", maps_api_key=settings.google_maps_api_key)
        conn.execute(
            "UPDATE activity SET body = %s, detail = detail || %s WHERE lead_id = %s AND type = 'created'",
            (summary + (f". {note}" if note else ""), json.dumps({"call": summary, "recording": data.get("recording_player") or data.get("recording")}), result.lead_id),
        )
        if real:
            # An answered call counts as first contact: the client has already spoken to them. Logging the
            # touch cancels the pending new-lead alert, so put one back: they still want to know it's in.
            lead = conn.execute("SELECT * FROM lead WHERE id = %s", (result.lead_id,)).fetchone()
            lead_svc.log_touch(conn, lead, ActivityType.CALL_ATTEMPT, actor_id=None, actor_label="CallRail", note=f"Spoke on the phone, {_mmss(duration)}", detail={"kind": "inbound_call", "callrail_call_id": call_id})
            followup.schedule(conn, tenant_id=tenant["id"], lead_id=lead["id"], user_id=lead.get("assigned_to"), rule="new_alert", due_at=datetime.now(UTC), key=f"{lead['id']}:new_alert:call:{call_id or datetime.now(UTC).timestamp()}")
    background.add_task(worker.dispatch_lead_now, result.lead_id)
    log.info("callrail %s ref=%s created=%s %s", slug, result.ref_code, result.created, summary)
    return JSONResponse({"ok": True, "lead_id": str(result.lead_id), "ref": result.ref_code, "created": result.created, "call": summary}, status_code=201 if result.created else 200)


# --- Meta Lead Ads -------------------------------------------------------------------------------

@router.get("/meta/webhook")
def meta_verify(request: Request):
    q = request.query_params
    settings = get_settings()
    if q.get("hub.mode") == "subscribe" and settings.meta_verify_token and hmac.compare_digest(q.get("hub.verify_token", ""), settings.meta_verify_token):
        return PlainTextResponse(q.get("hub.challenge", ""))
    raise HTTPException(status_code=403, detail="verification failed")


def fetch_meta_lead(leadgen_id: str, access_token: str) -> dict[str, Any]:
    """The webhook only carries ids; the answers come from the Graph API."""
    r = httpx.get(
        f"{GRAPH}/{leadgen_id}",
        params={"access_token": access_token, "fields": "id,created_time,field_data,form_id,ad_id,ad_name,adset_id,adset_name,campaign_id,campaign_name,platform,is_organic"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def meta_fields(lead: dict[str, Any]) -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for item in lead.get("field_data") or []:
        name = str(item.get("name") or "").strip()
        values = item.get("values") or []
        if name and values:
            flat[name] = ", ".join(str(v) for v in values if v is not None)
    # Meta's standard question names
    for theirs, ours in (("full_name", "name"), ("phone_number", "phone"), ("email", "email"), ("street_address", "contact_address"), ("city", "city"), ("province", "province"), ("state", "province"), ("zip_code", "postal_code"), ("post_code", "postal_code"), ("company_name", "company")):
        if theirs in flat and ours not in flat:
            flat[ours] = flat.pop(theirs)
    flat.update({
        "channel": "meta_lead_ad", "meta_lead_id": lead.get("id"), "form_name": lead.get("form_id"),
        "campaign_id": lead.get("campaign_id"), "campaign_name": lead.get("campaign_name"), "adset_id": lead.get("adset_id"), "ad_id": lead.get("ad_id"),
    })
    return flat


@router.post("/meta/webhook")
async def meta_webhook(request: Request, background: BackgroundTasks):
    settings = get_settings()
    raw = await request.body()
    if not settings.meta_app_secret:
        raise HTTPException(status_code=503, detail="Meta is not configured")
    sent = request.headers.get("x-hub-signature-256", "")
    want = "sha256=" + hmac.new(settings.meta_app_secret.encode(), raw, hashlib.sha256).hexdigest()
    if not sent or not hmac.compare_digest(sent, want):
        raise HTTPException(status_code=403, detail="bad signature")
    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="invalid JSON") from exc
    if body.get("object") != "page":
        return JSONResponse({"ok": True, "ignored": body.get("object")})
    if not settings.meta_access_token:
        log.error("meta leadgen webhook received but META_ACCESS_TOKEN is not set")
        raise HTTPException(status_code=503, detail="Meta access token is not configured")

    handled: list[dict[str, Any]] = []
    failures = 0
    for entry in body.get("entry") or []:
        page_id = str(entry.get("id") or "")
        for change in entry.get("changes") or []:
            if change.get("field") != "leadgen":
                continue
            value = change.get("value") or {}
            leadgen_id = str(value.get("leadgen_id") or "")
            page_id = str(value.get("page_id") or page_id)
            if not leadgen_id:
                continue
            with system_db() as conn:
                tenant = conn.execute("SELECT * FROM tenant WHERE is_active AND settings -> 'meta' ->> 'page_id' = %s", (page_id,)).fetchone()
                if tenant is None:
                    log.warning("meta lead %s for unknown page %s", leadgen_id, page_id)
                    handled.append({"leadgen_id": leadgen_id, "ignored": "no client has this page id"})
                    continue
                if conn.execute("SELECT 1 FROM lead_source WHERE tenant_id = %s AND meta_lead_id = %s", (tenant["id"], leadgen_id)).fetchone():
                    handled.append({"leadgen_id": leadgen_id, "ignored": "already received"})
                    continue
                try:
                    detail = fetch_meta_lead(leadgen_id, settings.meta_access_token)
                except (httpx.HTTPError, ValueError) as exc:
                    log.error("meta lead %s fetch failed: %s", leadgen_id, exc)
                    failures += 1
                    continue
                flat = meta_fields(detail)
                fields = attribution.interpret(flat, tenant.get("field_map") or {}, default_channel=Channel.META_LEAD_AD)
                fields.channel = Channel.META_LEAD_AD
                fields.platform = Platform.META_ADS
                try:
                    result = lead_svc.create_from_intake(conn, tenant, fields, raw_payload={**detail, "webhook": value}, ip=None, user_agent="meta-leadgen", maps_api_key=settings.google_maps_api_key)
                except lead_svc.LeadError as exc:
                    log.warning("meta lead %s unusable: %s", leadgen_id, exc)
                    handled.append({"leadgen_id": leadgen_id, "ignored": str(exc)})
                    continue
            background.add_task(worker.dispatch_lead_now, result.lead_id)
            handled.append({"leadgen_id": leadgen_id, "lead_id": str(result.lead_id), "ref": result.ref_code, "created": result.created})
    if failures and not handled:
        raise HTTPException(status_code=502, detail="could not fetch the lead from Meta; will retry")  # Meta retries non-2xx
    return JSONResponse({"ok": True, "leads": handled, "failed": failures})


# --- Google Ads lead forms -------------------------------------------------------------------------

GOOGLE_COLUMNS = {
    "FULL_NAME": "name", "FIRST_NAME": "first_name", "LAST_NAME": "last_name", "EMAIL": "email", "PHONE_NUMBER": "phone",
    "POSTAL_CODE": "postal_code", "CITY": "city", "REGION": "province", "COUNTRY": "country", "STREET_ADDRESS": "contact_address",
    "COMPANY_NAME": "company", "JOB_TITLE": "job_title", "WORK_EMAIL": "email", "WORK_PHONE": "phone",
}


@router.post("/google-leads/{slug}/{key}")
async def google_lead_form(slug: str, key: str, request: Request, background: BackgroundTasks):
    settings = get_settings()
    data = await _json_body(request)
    with system_db() as conn:
        tenant = conn.execute("SELECT * FROM tenant WHERE slug = %s AND is_active", (slug,)).fetchone()
        google_key = str(data.get("google_key") or "")
        if tenant is None or not (hmac.compare_digest(tenant["intake_key"], key) or (google_key and hmac.compare_digest(tenant["intake_key"], google_key))):
            raise HTTPException(status_code=404, detail="unknown endpoint")
        flat: dict[str, Any] = {"channel": "google_lead_form"}
        for col in data.get("user_column_data") or []:
            cid = str(col.get("column_id") or "").strip()
            name = str(col.get("column_name") or "").strip()
            value = col.get("string_value")
            if value in (None, ""):
                continue
            flat[GOOGLE_COLUMNS.get(cid) or name or cid] = str(value)
        flat.update({
            "google_lead_id": data.get("lead_id"), "campaign_id": data.get("campaign_id"), "adset_id": data.get("adgroup_id"),
            "ad_id": data.get("creative_id"), "form_name": data.get("form_id"), "gclid": data.get("gcl_id"),
        })
        fields = attribution.interpret({k: v for k, v in flat.items() if v not in (None, "")}, tenant.get("field_map") or {}, default_channel=Channel.GOOGLE_LEAD_FORM)
        fields.channel = Channel.GOOGLE_LEAD_FORM
        fields.platform = Platform.GOOGLE_ADS
        is_test = data.get("is_test") in (True, "true", "1", 1)
        if is_test and fields.contact_name and not fields.contact_name.upper().startswith("TEST"):
            fields.contact_name = f"TEST {fields.contact_name}"
        try:
            result = lead_svc.create_from_intake(
                conn, tenant, fields, raw_payload={**data, "is_test": is_test}, ip=None, user_agent="google-lead-form",
                maps_api_key=settings.google_maps_api_key, extra_flags={"test": True} if is_test else None,
            )
        except lead_svc.LeadError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    background.add_task(worker.dispatch_lead_now, result.lead_id)
    log.info("google lead form %s ref=%s created=%s test=%s", slug, result.ref_code, result.created, is_test)
    return JSONResponse({"ok": True, "lead_id": str(result.lead_id), "ref": result.ref_code, "created": result.created, "test": is_test}, status_code=201 if result.created else 200)


@router.get("/google-leads/{slug}/{key}")
@router.get("/callrail/{slug}/{key}")
def source_get():
    return Response(content='{"ok": true, "detail": "POST the webhook payload here."}', media_type="application/json")

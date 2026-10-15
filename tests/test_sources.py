"""CallRail, Meta Lead Ads and Google lead-form webhooks, and the attribution snippet."""

import base64
import hashlib
import hmac
import json

import pytest

from app.routers import sources


@pytest.fixture
def setup(make_tenant, make_user, admin_conn):
    t = make_tenant("roofco", name="North Ridge Roofing", twilio_number="+17055550000",
                    business_hours={"start": "00:00", "end": "23:59", "days": [1, 2, 3, 4, 5, 6, 7]},
                    settings={"meta": {"page_id": "111222333"}})
    owner = make_user("dan@roofco.test", tenant_id=t["id"], role="owner", name="Dan Owner", mobile="+17055551000")
    admin_conn.execute("UPDATE tenant SET default_assignee_id = %s WHERE id = %s", (owner["id"], t["id"]))
    return admin_conn.execute("SELECT * FROM tenant WHERE id = %s", (t["id"],)).fetchone()


def _call(answered=True, duration=125, **extra):
    return {
        "id": "CAL123", "answered": answered, "duration": duration, "direction": "inbound", "customer_phone_number": "+17055550142",
        "customer_name": "MIKE REYNOLDS", "customer_city": "Bracebridge", "customer_state": "ON", "source": "Google Ads", "medium": "cpc",
        "campaign": "Roofing - Muskoka", "keywords": "roof repair near me", "landing_page_url": "https://northridge.ca/roofing?gclid=abc",
        "gclid": "Cj0abc", "recording_player": "https://app.callrail.com/calls/CAL123/recording", "tags": [], "tracking_phone_number": "+17055559999",
        **extra,
    }


def test_callrail_answered_call_becomes_contacted_lead_with_attribution(client, setup, admin_conn, outbox):
    t = setup
    r = client.post(f"/v1/callrail/{t['slug']}/{t['intake_key']}", json=_call())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["created"] is True and body["call"] == "Answered call, 2 min 5 s"
    lead = admin_conn.execute("SELECT * FROM lead WHERE id = %s", (body["lead_id"],)).fetchone()
    assert lead["channel"] == "call" and lead["platform"] == "google_ads" and lead["phone_e164"] == "+17055550142" and lead["contact_name"] == "MIKE REYNOLDS"
    assert lead["status"] == "contacted" and lead["first_contact_at"] is not None  # they spoke; that is contact
    src = admin_conn.execute("SELECT * FROM lead_source WHERE lead_id = %s", (lead["id"],)).fetchone()
    assert src["callrail_call_id"] == "CAL123" and src["gclid"] == "Cj0abc" and src["utm_term"] == "roof repair near me" and src["raw_payload"]["recording_player"].endswith("/recording")
    # The client still hears about it, and no acknowledgement text goes to a caller.
    assert any("New lead: MIKE REYNOLDS (A001)" in m["body"] for m in outbox.sms if m["to"] == "+17055551000")
    assert not [m for m in outbox.sms if m["to"] == "+17055550142"]
    page_types = [a["type"] for a in admin_conn.execute("SELECT type FROM activity WHERE lead_id = %s ORDER BY id", (lead["id"],))]
    assert "call_attempt" in page_types


def test_callrail_missed_call_is_flagged_and_alerts(client, setup, admin_conn, outbox):
    t = setup
    r = client.post(f"/v1/callrail/{t['slug']}/{t['intake_key']}", json=_call(answered=False, duration=0, id="CAL9", source="Direct", medium="", gclid="", landing_page_url=""))
    assert r.status_code == 201 and r.json()["call"] == "Missed call"
    lead = admin_conn.execute("SELECT * FROM lead WHERE id = %s", (r.json()["lead_id"],)).fetchone()
    assert lead["flags"].get("missed_call") is True and lead["status"] == "new" and lead["platform"] == "direct"
    alert = next(m for m in outbox.sms if m["to"] == "+17055551000")
    assert "MISSED CALL, call back now" in alert["body"]
    # A 12-second answered call is treated the same way.
    r = client.post(f"/v1/callrail/{t['slug']}/{t['intake_key']}", json=_call(answered=True, duration=12, id="CAL10", customer_phone_number="+17055550177"))
    assert r.json()["call"] == "Short call, 12 s"
    assert admin_conn.execute("SELECT flags FROM lead WHERE id = %s", (r.json()["lead_id"],)).fetchone()["flags"].get("missed_call") is True


def test_callrail_call_from_existing_lead_logs_on_it(client, setup, admin_conn, outbox):
    t = setup
    lead_id = client.post(f"/v1/intake/{t['slug']}/{t['intake_key']}", json={"name": "Mike", "phone": "7055550142"}).json()["lead_id"]
    outbox.clear()
    r = client.post(f"/v1/callrail/{t['slug']}/{t['intake_key']}", json=_call(answered=False, duration=0, id="CAL77"))
    assert r.status_code == 200 and r.json()["created"] is False and r.json()["lead_id"] == lead_id
    assert admin_conn.execute("SELECT count(*) AS n FROM activity WHERE lead_id = %s AND type = 'lead_replied'", (lead_id,)).fetchone()["n"] == 1
    assert any("replied" in m["body"] and "Missed call from them" in m["body"] for m in outbox.sms)
    assert admin_conn.execute("SELECT count(*) AS n FROM lead_source WHERE lead_id = %s", (lead_id,)).fetchone()["n"] == 2
    # An answered call later is contact.
    client.post(f"/v1/callrail/{t['slug']}/{t['intake_key']}", json=_call(answered=True, duration=300, id="CAL78"))
    lead = admin_conn.execute("SELECT status FROM lead WHERE id = %s", (lead_id,)).fetchone()
    assert lead["status"] == "contacted"


def test_callrail_drops_spam_and_outbound(client, setup, admin_conn):
    t = setup
    r = client.post(f"/v1/callrail/{t['slug']}/{t['intake_key']}", json=_call(tags=["Spam"]))
    assert r.status_code == 200 and "spam" in r.json()["ignored"]
    r = client.post(f"/v1/callrail/{t['slug']}/{t['intake_key']}", json=_call(direction="outbound"))
    assert r.json()["ignored"] == "outbound call"
    assert admin_conn.execute("SELECT count(*) AS n FROM lead").fetchone()["n"] == 0
    assert client.post(f"/v1/callrail/{t['slug']}/wrong", json=_call()).status_code == 404


def test_callrail_signature_when_configured(client, setup, monkeypatch):
    from app import config

    s = config.get_settings()
    monkeypatch.setattr(s, "callrail_signing_key", "cr-secret")
    t = setup
    body = json.dumps(_call()).encode()
    bad = client.post(f"/v1/callrail/{t['slug']}/{t['intake_key']}", content=body, headers={"Content-Type": "application/json"})
    assert bad.status_code == 403
    sig = base64.b64encode(hmac.new(b"cr-secret", body, hashlib.sha1).digest()).decode()
    ok = client.post(f"/v1/callrail/{t['slug']}/{t['intake_key']}", content=body, headers={"Content-Type": "application/json", "Signature": sig})
    assert ok.status_code == 201


def _meta_post(client, payload: dict, secret: str = "meta-secret"):
    raw = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return client.post("/v1/meta/webhook", content=raw, headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig})


def test_meta_webhook_verification_and_signature(client, setup):
    r = client.get("/v1/meta/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "12345"})
    assert r.status_code == 200 and r.text == "12345"
    assert client.get("/v1/meta/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "1"}).status_code == 403
    assert _meta_post(client, {"object": "page", "entry": []}, secret="wrong").status_code == 403


def test_meta_lead_is_fetched_and_created(client, setup, admin_conn, outbox, monkeypatch):
    fetched = {}

    def fake_fetch(leadgen_id, token):
        fetched["id"] = leadgen_id
        fetched["token"] = token
        return {
            "id": leadgen_id, "created_time": "2026-10-15T14:00:00+0000", "form_id": "555", "ad_id": "ad1", "adset_id": "as1",
            "campaign_id": "c1", "campaign_name": "Roofing Leads - Muskoka", "platform": "fb",
            "field_data": [
                {"name": "full_name", "values": ["Sara Lee"]}, {"name": "phone_number", "values": ["+17055550155"]},
                {"name": "email", "values": ["sara@example.com"]}, {"name": "what_do_you_need_done?", "values": ["Metal roof"]},
                {"name": "city", "values": ["Huntsville"]},
            ],
        }

    monkeypatch.setattr(sources, "fetch_meta_lead", fake_fetch)
    payload = {"object": "page", "entry": [{"id": "111222333", "time": 1, "changes": [{"field": "leadgen", "value": {"leadgen_id": "LG1", "page_id": "111222333", "form_id": "555", "created_time": 1}}]}]}
    r = _meta_post(client, payload)
    assert r.status_code == 200, r.text
    assert fetched == {"id": "LG1", "token": "EAAtest"}
    info = r.json()["leads"][0]
    assert info["created"] is True
    lead = admin_conn.execute("SELECT * FROM lead WHERE id = %s", (info["lead_id"],)).fetchone()
    assert lead["channel"] == "meta_lead_ad" and lead["platform"] == "meta_ads" and lead["contact_name"] == "Sara Lee" and lead["email"] == "sara@example.com"
    assert lead["campaign_name"] == "Roofing Leads - Muskoka"
    src = admin_conn.execute("SELECT * FROM lead_source WHERE lead_id = %s", (lead["id"],)).fetchone()
    assert src["meta_lead_id"] == "LG1" and src["campaign_id"] == "c1" and src["adset_id"] == "as1" and src["ad_id"] == "ad1"
    assert src["raw_payload"]["field_data"][3]["values"] == ["Metal roof"]  # custom questions kept for the field map
    assert any("New lead: Sara Lee (A001)" in m["body"] for m in outbox.sms)
    assert not [m for m in outbox.sms if m["to"] == "+17055550155"]  # no ack to Meta leads without the consent switch
    # Meta re-delivers; the second copy is ignored.
    r = _meta_post(client, payload)
    assert r.json()["leads"][0]["ignored"] == "already received"
    # Unknown page: logged, not an error (so Meta stops retrying).
    r = _meta_post(client, {"object": "page", "entry": [{"id": "999", "changes": [{"field": "leadgen", "value": {"leadgen_id": "LG2", "page_id": "999"}}]}]})
    assert r.status_code == 200 and "no client" in r.json()["leads"][0]["ignored"]


def test_meta_fetch_failure_asks_for_retry(client, setup, monkeypatch):
    import httpx

    def boom(leadgen_id, token):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(sources, "fetch_meta_lead", boom)
    r = _meta_post(client, {"object": "page", "entry": [{"id": "111222333", "changes": [{"field": "leadgen", "value": {"leadgen_id": "LG3", "page_id": "111222333"}}]}]})
    assert r.status_code == 502


def test_google_lead_form_webhook(client, setup, admin_conn, outbox):
    t = setup
    payload = {
        "lead_id": "GL123", "api_version": "1.0", "form_id": 1001, "campaign_id": 2002, "adgroup_id": 3003, "creative_id": 4004,
        "gcl_id": "Cj0google", "google_key": t["intake_key"], "is_test": False,
        "user_column_data": [
            {"column_name": "Full Name", "string_value": "Tom Baker", "column_id": "FULL_NAME"},
            {"column_name": "User Phone", "string_value": "+17055550166", "column_id": "PHONE_NUMBER"},
            {"column_name": "User Email", "string_value": "tom@example.com", "column_id": "EMAIL"},
            {"column_name": "Postal Code", "string_value": "P1L 1A1", "column_id": "POSTAL_CODE"},
            {"column_name": "What work do you need?", "string_value": "Roof replacement", "column_id": "what_work_do_you_need"},
        ],
    }
    r = client.post(f"/v1/google-leads/{t['slug']}/{t['intake_key']}", json=payload)
    assert r.status_code == 201, r.text
    lead = admin_conn.execute("SELECT * FROM lead WHERE id = %s", (r.json()["lead_id"],)).fetchone()
    assert lead["channel"] == "google_lead_form" and lead["platform"] == "google_ads" and lead["contact_name"] == "Tom Baker" and lead["phone_e164"] == "+17055550166"
    src = admin_conn.execute("SELECT * FROM lead_source WHERE lead_id = %s", (lead["id"],)).fetchone()
    assert src["google_lead_id"] == "GL123" and src["gclid"] == "Cj0google" and src["campaign_id"] == "2002" and src["meta_lead_id"] is None
    # Google's "send test data" button: the key may come only in the body; the lead is marked and gets no messages.
    outbox.clear()
    test = {**payload, "lead_id": "GLTEST", "is_test": True, "user_column_data": [{"column_name": "Full Name", "string_value": "Jane Doe", "column_id": "FULL_NAME"}, {"column_name": "Phone", "string_value": "+17055550199", "column_id": "PHONE_NUMBER"}]}
    r = client.post(f"/v1/google-leads/{t['slug']}/anything", json=test)
    assert r.status_code == 201 and r.json()["test"] is True
    lead = admin_conn.execute("SELECT contact_name, flags FROM lead WHERE id = %s", (r.json()["lead_id"],)).fetchone()
    assert lead["contact_name"] == "TEST Jane Doe" and lead["flags"].get("test") is True
    assert not [m for m in outbox.sms if m["to"] == "+17055550199"]
    assert client.post(f"/v1/google-leads/{t['slug']}/wrong", json={**payload, "google_key": "wrong"}).status_code == 404


def test_attribution_snippet_is_served(client):
    r = client.get("/static/wmt.js")
    assert r.status_code == 200 and "window.wmt" in r.text and "gclid" in r.text and "form_fields" in r.text

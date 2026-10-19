"""Conversion uploads to Google Ads and Meta, retries, and the nightly spend pull."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.db import system_db
from app.models import ActivityType, LeadStatus
from app.services import adplatforms, conversions
from app.services import leads as svc
from tests.conftest import csrf_from

TOR = ZoneInfo("America/Toronto")


@pytest.fixture
def setup(make_tenant, make_user, admin_conn):
    t = make_tenant(
        "roofco", name="North Ridge Roofing",
        settings={
            "google_ads": {"customer_id": "1234567890", "qualified_action": "111", "won_action": "customers/1234567890/conversionActions/222", "qualified_value": 50},
            "meta": {"page_id": "9", "dataset_id": "555000", "ad_account_id": "777000"},
        },
    )
    owner = make_user("dan@roofco.test", tenant_id=t["id"], role="owner", name="Dan", mobile="+17055551000")
    admin_conn.execute("UPDATE tenant SET default_assignee_id = %s WHERE id = %s", (owner["id"], t["id"]))
    return admin_conn.execute("SELECT * FROM tenant WHERE id = %s", (t["id"],)).fetchone()


def _url(t):
    return f"/v1/intake/{t['slug']}/{t['intake_key']}"


def _events(admin_conn, lead_id):
    return admin_conn.execute("SELECT platform, action, status, value, last_error FROM conversion_event WHERE lead_id = %s ORDER BY platform, action", (lead_id,)).fetchall()


def test_contact_queues_qualified_and_the_tick_uploads_it(client, setup, admin_conn, outbox):
    t = setup
    lead_id = client.post(_url(t), json={"name": "Mike Reynolds", "phone": "705 555 0142", "email": "Mike.Reynolds@Gmail.com", "gclid": "Cj0abc"}).json()["lead_id"]
    with system_db() as conn:
        svc.log_touch(conn, svc.get_lead(conn, lead_id), ActivityType.CALL_ATTEMPT, actor_id=None, actor_label="Dan")
    evs = _events(admin_conn, lead_id)
    assert [(e["platform"], e["action"], e["status"]) for e in evs] == [("google_ads", "qualified", "pending")]
    assert evs[0]["value"] == 50
    with system_db() as conn:
        stats = conversions.process_pending(conn)
    assert stats["sent"] == 1
    up = adplatforms.outbox.google_conversions[0]
    assert up["customer_id"] == "1234567890" and up["conversionAction"] == "customers/1234567890/conversionActions/111"
    assert up["gclid"] == "Cj0abc" and up["conversionValue"] == 50.0 and up["currencyCode"] == "CAD" and up["orderId"] == f"{lead_id}:qualified"
    assert up["userIdentifiers"] == [{"hashedEmail": adplatforms.sha256("mikereynolds@gmail.com")}, {"hashedPhoneNumber": adplatforms.sha256("+17055550142")}]
    assert up["conversionDateTime"].endswith(("-04:00", "-05:00"))
    assert _events(admin_conn, lead_id)[0]["status"] == "sent"
    act = admin_conn.execute("SELECT body, detail FROM activity WHERE lead_id = %s AND type = 'conversion_sent'", (lead_id,)).fetchone()
    assert act["body"] == "Qualified lead → Google Ads ($50)" and act["detail"]["status"] == "sent"
    # Quoted later does not send qualified twice.
    with system_db() as conn:
        svc.change_status(conn, svc.get_lead(conn, lead_id), LeadStatus.QUOTED, actor_id=None, actor_label="Dan")
    assert len(_events(admin_conn, lead_id)) == 1


def test_won_sends_closed_won_with_value_to_both_platforms(client, setup, admin_conn, outbox):
    t = setup
    lead_id = client.post(_url(t), json={"name": "Sara", "phone": "7055550155", "email": "sara@example.com", "fbclid": "IwAR1", "_fbp": "fb.1.123.456", "channel": "website_form"}).json()["lead_id"]
    with system_db() as conn:
        svc.change_status(conn, svc.get_lead(conn, lead_id), LeadStatus.WON, actor_id=None, actor_label="Dan", deal_value=Decimal("8500"))
    evs = _events(admin_conn, lead_id)
    # A Meta-attributed lead goes to Meta only (platform meta_ads), both actions queued at once.
    assert [(e["platform"], e["action"]) for e in evs] == [("meta_ads", "qualified"), ("meta_ads", "won")]
    with system_db() as conn:
        conversions.process_pending(conn)
    sent = adplatforms.outbox.meta_events
    assert [e["event_name"] for e in sent] == ["Qualified", "Converted"]
    won = sent[1]
    assert won["dataset_id"] == "555000" and won["action_source"] == "system_generated" and won["custom_data"]["value"] == 8500.0
    assert won["user_data"]["fbp"] == "fb.1.123.456" and won["user_data"]["em"] == [adplatforms.sha256("sara@example.com")] and won["user_data"]["ph"] == [adplatforms.sha256("17055550155")]
    assert won["custom_data"]["event_source"] == "crm" and won["event_id"] == f"{lead_id}:won"


def test_website_lead_without_click_id_still_uploads_by_hashed_identifiers(client, setup, admin_conn):
    t = setup
    lead_id = client.post(_url(t), json={"name": "Organic Olive", "email": "olive@example.com"}).json()["lead_id"]
    with system_db() as conn:
        svc.change_status(conn, svc.get_lead(conn, lead_id), LeadStatus.CONTACTED, actor_id=None, actor_label="Dan")
        conversions.process_pending(conn)
    up = adplatforms.outbox.google_conversions[0]
    assert "gclid" not in up and up["userIdentifiers"] == [{"hashedEmail": adplatforms.sha256("olive@example.com")}]


def test_imported_and_test_leads_never_upload(client, setup, admin_conn):
    t = setup
    admin_conn.execute(
        "INSERT INTO lead (tenant_id, ref_code, contact_name, email, channel, platform, flags, status) VALUES (%s, 'A900', 'Old', 'old@example.com', 'import', 'google_ads', '{\"imported\": true}', 'contacted')", (t["id"],)
    )
    old = admin_conn.execute("SELECT * FROM lead WHERE ref_code = 'A900'").fetchone()
    with system_db() as conn:
        svc.change_status(conn, svc.get_lead(conn, old["id"]), LeadStatus.WON, actor_id=None, actor_label="Dan", deal_value=Decimal("1000"))
    assert _events(admin_conn, old["id"]) == []
    test_id = client.post(f"/v1/google-leads/{t['slug']}/{t['intake_key']}", json={"lead_id": "GLT", "google_key": t["intake_key"], "is_test": True, "user_column_data": [{"column_id": "FULL_NAME", "string_value": "Jane"}, {"column_id": "EMAIL", "string_value": "j@example.com"}]}).json()["lead_id"]
    with system_db() as conn:
        svc.change_status(conn, svc.get_lead(conn, test_id), LeadStatus.CONTACTED, actor_id=None, actor_label="Dan")
    assert _events(admin_conn, test_id) == []


def test_failures_retry_with_backoff_then_give_up_visibly(client, setup, admin_conn, monkeypatch):
    t = setup
    lead_id = client.post(_url(t), json={"name": "Flaky", "phone": "7055550111", "gclid": "x"}).json()["lead_id"]
    with system_db() as conn:
        svc.change_status(conn, svc.get_lead(conn, lead_id), LeadStatus.CONTACTED, actor_id=None, actor_label="Dan")
    calls = {"n": 0}

    def flaky(customer_id, conversion):
        calls["n"] += 1
        raise adplatforms.AdsError("Google Ads 503: try later", retryable=True)

    monkeypatch.setattr(adplatforms, "google_upload_conversion", flaky)
    now = datetime.now(UTC)
    with system_db() as conn:
        s1 = conversions.process_pending(conn, now=now)
        s2 = conversions.process_pending(conn, now=now + timedelta(minutes=1))   # inside the backoff: not retried
        s3 = conversions.process_pending(conn, now=now + timedelta(minutes=6))   # after 5 min: retried
    assert (s1["deferred"], s2["deferred"], s3["deferred"]) == (1, 1, 1) and calls["n"] == 2
    ev = _events(admin_conn, lead_id)[0]
    assert ev["status"] == "pending" and "503" in ev["last_error"]
    # Still failing three days later: marked failed and written on the lead.
    with system_db() as conn:
        s4 = conversions.process_pending(conn, now=now + timedelta(hours=73))
    assert s4["failed"] == 1 and _events(admin_conn, lead_id)[0]["status"] == "failed"
    note = admin_conn.execute("SELECT body FROM activity WHERE lead_id = %s AND type = 'conversion_sent'", (lead_id,)).fetchone()
    assert note["body"].startswith("Qualified lead → Google Ads: not sent")
    # A permanent rejection fails at once.
    lead2 = client.post(_url(t), json={"name": "Rejected", "phone": "7055550112", "gclid": "old"}).json()["lead_id"]
    monkeypatch.setattr(adplatforms, "google_upload_conversion", lambda c, conv: (_ for _ in ()).throw(adplatforms.AdsError("CLICK_TOO_OLD", retryable=False)))
    with system_db() as conn:
        svc.change_status(conn, svc.get_lead(conn, lead2), LeadStatus.CONTACTED, actor_id=None, actor_label="Dan")
        conversions.process_pending(conn)
    assert _events(admin_conn, lead2)[0]["status"] == "failed"


def test_agency_sees_sync_status_and_can_retry(client, setup, make_user, admin_conn, monkeypatch):
    import re

    import pyotp

    from app.auth import crypto

    t = setup
    lead_id = client.post(_url(t), json={"name": "Mike", "phone": "7055550142", "gclid": "x"}).json()["lead_id"]
    with system_db() as conn:
        svc.change_status(conn, svc.get_lead(conn, lead_id), LeadStatus.CONTACTED, actor_id=None, actor_label="Dan")
    admin_conn.execute("UPDATE conversion_event SET status = 'failed', last_error = 'boom' WHERE lead_id = %s", (lead_id,))
    u = make_user("jeff@agency.test", role="agency_admin", totp_enabled=True)
    secret = crypto.new_totp_secret()
    admin_conn.execute("UPDATE app_user SET totp_secret_enc = %s WHERE id = %s", (crypto.encrypt_secret(secret), u["id"]))
    client.post("/login", data={"email": "jeff@agency.test", "password": "correct-horse-battery"})
    page = client.get("/login/mfa")
    csrf = re.search(r'name="csrf_token" value="([0-9a-f]+)"', page.text).group(1)
    client.post("/login/mfa", data={"code": pyotp.TOTP(secret).now(), "csrf_token": csrf})
    client.get(f"/admin/tenants/{t['id']}/leads/{lead_id}")
    page = client.get(f"/leads/{lead_id}")
    assert "Ad platform sync" in page.text and "failed" in page.text and "Retry now" in page.text
    ev_id = admin_conn.execute("SELECT id FROM conversion_event WHERE lead_id = %s", (lead_id,)).fetchone()["id"]
    csrf = re.search(r'name="csrf_token" value="([0-9a-f]+)"', page.text).group(1)
    r = client.post(f"/admin/conversions/{ev_id}/retry", data={"csrf_token": csrf}, follow_redirects=False)
    assert r.status_code == 303
    assert _events(admin_conn, lead_id)[0]["status"] == "sent" and len(adplatforms.outbox.google_conversions) == 1
    # Settings page shows the ad platform fields and the recent upload.
    page = client.get(f"/admin/tenants/{t['id']}")
    assert 'name="google_customer_id"' in page.text and "Recent uploads" in page.text


def test_disabled_tenant_queues_nothing(client, setup, admin_conn):
    t = setup
    admin_conn.execute("""UPDATE tenant SET settings = settings || '{"conversions": {"enabled": false}}' WHERE id = %s""", (t["id"],))
    lead_id = client.post(_url(t), json={"name": "Quiet", "phone": "7055550142", "gclid": "x"}).json()["lead_id"]
    with system_db() as conn:
        svc.change_status(conn, svc.get_lead(conn, lead_id), LeadStatus.CONTACTED, actor_id=None, actor_label="Dan")
    assert _events(admin_conn, lead_id) == []


def test_nightly_spend_pull_once_a_day(setup, admin_conn):
    t = setup
    adplatforms.outbox.fake_spend["google_ads"] = [{"campaign_id": "c1", "campaign_name": "Roofing - Muskoka", "date": "2026-10-13", "spend": 123.45, "clicks": 40, "impressions": 900}, {"campaign_id": "c1", "campaign_name": "Roofing - Muskoka", "date": "2026-10-14", "spend": 99.1, "clicks": 31, "impressions": 700}]
    adplatforms.outbox.fake_spend["meta_ads"] = [{"campaign_id": "m1", "campaign_name": "Leads - Oct", "date": "2026-10-14", "spend": 45.0, "clicks": 12, "impressions": 2000}]
    at = datetime(2026, 10, 15, 4, 30, tzinfo=TOR).astimezone(UTC)
    with system_db() as conn:
        report = conversions.run_nightly(conn, now=at)
    assert report["tenants"] == 1 and report["rows"] == 3 and report["errors"] == []
    q = adplatforms.outbox.spend_queries
    assert {x["platform"] for x in q} == {"google_ads", "meta_ads"} and q[0]["since"] == date(2026, 10, 13) and q[0]["until"] == date(2026, 10, 14)
    rows = admin_conn.execute("SELECT platform, campaign_id, date, spend FROM ad_spend WHERE tenant_id = %s ORDER BY platform, date", (t["id"],)).fetchall()
    assert [(r["platform"], r["campaign_id"], str(r["date"]), float(r["spend"])) for r in rows] == [("google_ads", "c1", "2026-10-13", 123.45), ("google_ads", "c1", "2026-10-14", 99.1), ("meta_ads", "m1", "2026-10-14", 45.0)]
    # Second tick the same day: nothing. Before 4 am: nothing.
    adplatforms.outbox.spend_queries.clear()
    with system_db() as conn:
        assert conversions.run_nightly(conn, now=at + timedelta(minutes=1)) is None
        assert conversions.run_nightly(conn, now=datetime(2026, 10, 16, 2, 0, tzinfo=TOR).astimezone(UTC)) is None
    assert adplatforms.outbox.spend_queries == []
    # Restated spend overwrites.
    adplatforms.outbox.fake_spend["google_ads"] = [{"campaign_id": "c1", "campaign_name": "Roofing - Muskoka", "date": "2026-10-14", "spend": 101.0, "clicks": 32, "impressions": 710}]
    with system_db() as conn:
        conversions.run_nightly(conn, now=at + timedelta(days=1))
    assert float(admin_conn.execute("SELECT spend FROM ad_spend WHERE campaign_id = 'c1' AND date = '2026-10-14'").fetchone()["spend"]) == 101.0


def test_google_yaml_credentials_are_read(tmp_path, monkeypatch):
    from app import config

    y = tmp_path / "google-ads.yaml"
    y.write_text("developer_token: DEVTOKEN\nclient_id: 123.apps.googleusercontent.com\nclient_secret: 'sec'\nrefresh_token: \"1//refresh\"\nlogin_customer_id: 647-416-4373\nuse_proto_plus: True\n")
    monkeypatch.setattr(config.get_settings(), "google_ads_yaml", str(y))
    creds = adplatforms.google_creds()
    assert creds.developer_token == "DEVTOKEN" and creds.client_secret == "sec" and creds.refresh_token == "1//refresh" and creds.login_customer_id == "6474164373"


def _service_account_key():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(encoding=serialization.Encoding.PEM, format=serialization.PrivateFormat.PKCS8, encryption_algorithm=serialization.NoEncryption()).decode()
    key = {"type": "service_account", "project_id": "wmt-campaign-manager", "private_key_id": "kid123", "private_key": pem,
           "client_email": "ads-bot@wmt-campaign-manager.iam.gserviceaccount.com", "token_uri": "https://oauth2.googleapis.com/token"}
    return key, private.public_key()


def test_service_account_yaml_finds_the_mounted_key_and_signs_a_jwt(tmp_path, monkeypatch):
    import base64
    import json

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    from app import config

    key, public = _service_account_key()
    # The yaml names the Campaign Manager's path on its VM; here the key sits next to the yaml under the same name.
    y = tmp_path / "google-ads.yaml"
    y.write_text("developer_token: DEVTOKEN\njson_key_file_path: /home/campaigns/wmt-ads-manager/service_account.json\nlogin_customer_id: 647-416-4373\nuse_proto_plus: True\n")
    (tmp_path / "service_account.json").write_text(json.dumps(key))
    monkeypatch.setattr(config.get_settings(), "google_ads_yaml", str(y))
    monkeypatch.setattr(config.get_settings(), "google_ads_sa_key", None)
    creds = adplatforms.google_creds()
    assert creds.kind == "service account" and creds.identity == "ads-bot@wmt-campaign-manager.iam.gserviceaccount.com"
    assert creds.developer_token == "DEVTOKEN" and creds.login_customer_id == "6474164373" and creds.sa_key["private_key_id"] == "kid123"
    assert adplatforms.google_creds_source() == "google-ads.yaml, service account"

    # GOOGLE_ADS_SA_KEY wins when set (the Cloud Run mount path); a missing key file means no credentials.
    other = tmp_path / "elsewhere.json"
    other.write_text(json.dumps({**key, "private_key_id": "kid999"}))
    monkeypatch.setattr(config.get_settings(), "google_ads_sa_key", str(other))
    assert adplatforms.google_creds().sa_key["private_key_id"] == "kid999"
    monkeypatch.setattr(config.get_settings(), "google_ads_sa_key", str(tmp_path / "nope.json"))
    (tmp_path / "service_account.json").unlink()
    assert adplatforms.google_creds() is None and adplatforms.google_creds_source() is None

    # The assertion is a valid RS256 JWT for the Ads scope that Google's token endpoint will accept.
    jwt = adplatforms.service_account_assertion(key, now=1_800_000_000)
    head, body, sig = jwt.split(".")
    pad = lambda t: t + "=" * (-len(t) % 4)  # noqa: E731
    assert json.loads(base64.urlsafe_b64decode(pad(head))) == {"alg": "RS256", "typ": "JWT", "kid": "kid123"}
    claims = json.loads(base64.urlsafe_b64decode(pad(body)))
    assert claims == {"iss": key["client_email"], "scope": "https://www.googleapis.com/auth/adwords", "aud": "https://oauth2.googleapis.com/token", "iat": 1_800_000_000, "exp": 1_800_003_600}
    public.verify(base64.urlsafe_b64decode(pad(sig)), f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())


def test_service_account_token_exchange_uses_the_jwt_grant(monkeypatch):
    import httpx

    key, _ = _service_account_key()
    creds = adplatforms.GoogleCreds("DEVTOKEN", login_customer_id="6474164373", sa_key=key)
    seen = {}

    def fake_post(url, data=None, timeout=None, **kw):
        seen["url"], seen["data"] = url, data
        return httpx.Response(200, json={"access_token": "ya29.test", "expires_in": 3599, "token_type": "Bearer"})

    monkeypatch.setattr(adplatforms.httpx, "post", fake_post)
    adplatforms._token_cache.clear()
    assert adplatforms.google_access_token(creds) == "ya29.test"
    assert seen["url"] == "https://oauth2.googleapis.com/token" and seen["data"]["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
    assert seen["data"]["assertion"].count(".") == 2
    headers = adplatforms._google_headers(creds)
    assert headers["Authorization"] == "Bearer ya29.test" and headers["developer-token"] == "DEVTOKEN" and headers["login-customer-id"] == "6474164373"
    # Cached until it nears expiry.
    seen.clear()
    assert adplatforms.google_access_token(creds) == "ya29.test" and not seen
    adplatforms._token_cache.clear()

    def failing_post(url, data=None, timeout=None, **kw):
        return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Invalid JWT Signature."})

    monkeypatch.setattr(adplatforms.httpx, "post", failing_post)
    with pytest.raises(adplatforms.AdsError) as exc:
        adplatforms.google_access_token(creds)
    assert "service account" in str(exc.value) and "Invalid JWT Signature" in str(exc.value)
    adplatforms._token_cache.clear()


# --- The Google Ads connection check on the client page ---------------------------------------------------

def test_google_ads_check_lists_actions_and_flags_problems(client, make_tenant, make_user, admin_conn):
    from tests.test_dashboard import agency_login

    t = make_tenant("roofco", name="North Ridge Roofing")
    agency_login(client, make_user, admin_conn)
    csrf = csrf_from(client)
    adplatforms.outbox.fake_check = {
        "creds": "google-ads.yaml", "login_customer_id": "1112223333",
        "account": {"id": "5556667777", "name": "North Ridge Roofing", "currency": "CAD", "time_zone": "America/Toronto", "manager": False},
        "actions": [
            {"id": "900001", "name": "CRM Qualified Lead", "status": "ENABLED", "type": "UPLOAD_CLICKS", "category": "QUALIFIED_LEAD", "primary": False},
            {"id": "900002", "name": "CRM Closed Won", "status": "ENABLED", "type": "UPLOAD_CLICKS", "category": "CONVERTED_LEAD", "primary": True},
            {"id": "900003", "name": "Old import", "status": "REMOVED", "type": "UPLOAD_CLICKS", "category": "LEAD", "primary": False},
        ],
        "spend_7d": 412.5,
    }
    r = client.post(
        f"/admin/tenants/{t['id']}/google-ads/check",
        data={"google_customer_id": "555-666-7777", "google_qualified_action": "900001", "google_won_action": "customers/5556667777/conversionActions/900003", "csrf_token": csrf},
    )
    assert r.status_code == 200, r.text
    html = r.text
    assert "Connected." in html and "North Ridge Roofing" in html and "$412" in html and "1112223333" in html
    assert "CRM Qualified Lead" in html and "Qualified ✓" in html
    assert "Old import (900003) is removed" in html          # the configured Won id points at a removed action
    assert "Use for Qualified" in html and 'value="555-666-7777"' in html  # typed id kept on the page, not saved
    assert admin_conn.execute("SELECT settings->'google_ads' AS g FROM tenant WHERE id = %s", (t["id"],)).fetchone()["g"] is None

    # A manager account id is called out; a missing id is refused before any call.
    adplatforms.outbox.fake_check["account"]["manager"] = True
    html = client.post(f"/admin/tenants/{t['id']}/google-ads/check", data={"google_customer_id": "5556667777", "csrf_token": csrf}).text
    assert "manager (MCC) account" in html
    html = client.post(f"/admin/tenants/{t['id']}/google-ads/check", data={"google_customer_id": "", "csrf_token": csrf}).text
    assert "Not connected." in html and "10-digit" in html

    # A failure from Google comes back in plain words.
    adplatforms.outbox.fake_check = adplatforms.AdsError("USER_PERMISSION_DENIED: the MCC behind the refresh token does not manage this customer id", retryable=False)
    html = client.post(f"/admin/tenants/{t['id']}/google-ads/check", data={"google_customer_id": "5556667777", "csrf_token": csrf}).text
    assert "Not connected." in html and "does not manage this customer id" in html


def test_google_error_translation():
    import httpx

    body = {"error": {"code": 403, "message": "The caller does not have permission", "status": "PERMISSION_DENIED", "details": [
        {"@type": "type.googleapis.com/google.ads.googleads.v19.errors.GoogleAdsFailure", "errors": [
            {"errorCode": {"authorizationError": "USER_PERMISSION_DENIED"}, "message": "User doesn't have permission to access customer."}]}]}}
    r = httpx.Response(403, json=body)
    assert adplatforms._google_error(r) == "USER_PERMISSION_DENIED: the MCC behind the refresh token does not manage this customer id"
    r = httpx.Response(400, json={"error": {"code": 400, "message": "Request contains an invalid argument.", "status": "INVALID_ARGUMENT", "details": [{"errors": [{"errorCode": {"queryError": "BAD_FIELD_NAME"}, "message": "Unrecognized field"}]}]}})
    assert adplatforms._google_error(r) == "BAD_FIELD_NAME: Unrecognized field"
    assert adplatforms._google_error(httpx.Response(502, text="<html>bad gateway")).startswith("Google Ads 502")

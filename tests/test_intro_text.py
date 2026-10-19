"""The Text button pre-fills the client's first-text message (with the lead's ref for the relay), the first
time only, signed by whoever is texting; the wording is editable per client."""

from __future__ import annotations

import re
from urllib.parse import unquote

from psycopg.types.json import Jsonb

from tests.conftest import csrf_from


def _lead(client, tenant, **fields):
    data = {"name": "Karen Whitfield", "phone": "7055550001", "service": "Driveway paving", **fields}
    r = client.post(f"/v1/intake/{tenant['slug']}/{tenant['intake_key']}", json=data)
    assert r.status_code in (200, 201), r.text
    return r.json()["lead_id"]


def _sms_body(html: str) -> str:
    m = re.search(r'href="sms:\+1\d{10}\?&amp;body=([^"]*)"', html) or re.search(r'href="sms:\+1\d{10}\?&body=([^"]*)"', html)
    assert m, "no sms: link on the page"
    return unquote(m.group(1))


def test_text_button_prefills_intro_with_ref_for_the_relay(client, make_tenant, make_user):
    t = make_tenant("ital", name="Ital Paving", twilio_number="+12495550100")
    make_user("jb@ital.test", tenant_id=t["id"], role="member", name="Jeffrey Braun", mobile="+17055550199")
    lead_id = _lead(client, t)
    client.post("/login", data={"email": "jb@ital.test", "password": "correct-horse-battery"})
    html = client.get(f"/leads/{lead_id}").text
    body = _sms_body(html)
    assert body.startswith("A001 Hi Karen, thanks for reaching out about your driveway paving.")
    assert body.endswith("Thanks, Jeffrey Braun")
    # The in-app box carries the same wording, without the ref (it already knows the lead).
    box = re.search(r"<textarea name=\"body\"[^>]*>([^<]*)</textarea>", html).group(1)
    assert box.startswith("Hi Karen, thanks for reaching out about your driveway paving.") and "A001" not in box


def test_intro_only_the_first_time_and_uses_client_wording(client, make_tenant, make_user, admin_conn, outbox):
    t = make_tenant("ital", name="Ital Paving", twilio_number="+12495550100")
    make_user("jb@ital.test", tenant_id=t["id"], role="member", name="Jeffrey Braun", mobile="+17055550199")
    admin_conn.execute(
        "UPDATE tenant SET settings = settings || %s WHERE id = %s",
        (Jsonb({"lead_messaging": {"templates": {"intro": "Hey {first_name}, {your_first_name} here from {client_name} about {job}. Good time to talk?"}}}), t["id"]),
    )
    lead_id = _lead(client, t, name="Dave Larocque")
    client.post("/login", data={"email": "jb@ital.test", "password": "correct-horse-battery"})
    html = client.get(f"/leads/{lead_id}").text
    assert _sms_body(html) == "A001 Hey Dave, Jeffrey here from Ital Paving about your driveway paving. Good time to talk?"

    # Send one text from the app; from then on the button opens with just the ref and the box is blank.
    csrf = csrf_from(client)
    r = client.post(f"/leads/{lead_id}/text", data={"body": "On my way", "csrf_token": csrf})
    assert r.status_code == 200, r.text
    html = client.get(f"/leads/{lead_id}").text
    assert _sms_body(html) == "A001 "
    box = re.search(r"<textarea name=\"body\"[^>]*>([^<]*)</textarea>", html).group(1)
    assert box == ""


def test_relay_strips_the_ref_and_sends_the_intro_to_the_lead(client, make_tenant, make_user, admin_conn, outbox):
    from app.routers.twilio import twilio_signature

    t = make_tenant("ital", name="Ital Paving", twilio_number="+12495550100")
    make_user("jb@ital.test", tenant_id=t["id"], role="member", name="Jeffrey Braun", mobile="+17055550199")
    lead_id = _lead(client, t)
    outbox.clear()
    params = {"From": "+17055550199", "To": "+12495550100", "Body": "A001 Hi Karen, thanks for reaching out about your driveway paving. Good time to talk? Thanks, Jeffrey Braun", "MessageSid": "SM1"}
    r = client.post("/v1/twilio/sms", data=params, headers={"X-Twilio-Signature": twilio_signature("http://testserver/v1/twilio/sms", params, "test-twilio-token")})
    assert r.status_code == 200, r.text
    sent = [m for m in outbox.sms if m["to"] == "+17055550001"]
    assert len(sent) == 1 and sent[0]["body"].startswith("Hi Karen, thanks") and "A001" not in sent[0]["body"]
    assert sent[0]["from"] == "+12495550100"
    assert admin_conn.execute("SELECT status FROM lead WHERE id = %s", (lead_id,)).fetchone()["status"] == "contacted"


def _owner_login(client, make_user, admin_conn, tenant_id, email="dan@ital.test", name="Jeffrey Braun"):
    import pyotp

    from app.auth import crypto

    u = make_user(email, tenant_id=tenant_id, role="owner", name=name, totp_enabled=True, mobile="+17055550199")
    secret = crypto.new_totp_secret()
    admin_conn.execute("UPDATE app_user SET totp_secret_enc = %s WHERE id = %s", (crypto.encrypt_secret(secret), u["id"]))
    client.post("/login", data={"email": email, "password": "correct-horse-battery"})
    page = client.get("/login/mfa")
    csrf = re.search(r'name="csrf_token" value="([0-9a-f]+)"', page.text).group(1)
    client.post("/login/mfa", data={"code": pyotp.TOTP(secret).now(), "csrf_token": csrf})
    return u


def test_owner_edits_first_text_on_the_messages_page(client, make_tenant, make_user, admin_conn):
    t = make_tenant("ital", name="Ital Paving", twilio_number="+12495550100")
    _owner_login(client, make_user, admin_conn, t["id"])
    lead_id = _lead(client, t)

    page = client.get("/messages")
    assert page.status_code == 200 and "Text wording" in page.text
    assert "Hi Karen, thanks for reaching out about your driveway paving." in page.text  # preview on the newest lead
    assert "Reset to standard" not in page.text  # still on the default
    csrf = csrf_from(client)

    # A broken placeholder is refused with the draft kept.
    r = client.post("/messages", data={"template": "Hi {first_name", "csrf_token": csrf})
    assert r.status_code == 400 and "problem near a {placeholder}" in r.text and "Hi {first_name" in r.text

    r = client.post("/messages", data={"template": "Hey {first_name}, {your_first_name} from {client_name} here. Sealing special on this month. Good time to talk about {job}?", "csrf_token": csrf}, follow_redirects=False)
    assert r.status_code == 303 and "Saved" in r.headers["location"]
    saved = admin_conn.execute("SELECT settings->'lead_messaging'->'templates'->>'intro' AS t FROM tenant WHERE id = %s", (t["id"],)).fetchone()["t"]
    assert saved.startswith("Hey {first_name}")
    html = client.get(f"/leads/{lead_id}").text
    assert _sms_body(html) == "A001 Hey Karen, Jeffrey from Ital Paving here. Sealing special on this month. Good time to talk about your driveway paving?"
    assert 'href="/messages"' in html  # owners get the "change it" link on the lead
    assert "Reset to standard" in client.get("/messages").text

    r = client.post("/messages", data={"template": "ignored", "action": "reset", "csrf_token": csrf}, follow_redirects=False)
    assert r.status_code == 303
    assert admin_conn.execute("SELECT settings->'lead_messaging'->'templates'->>'intro' AS t FROM tenant WHERE id = %s", (t["id"],)).fetchone()["t"] is None
    assert "Hi Karen, thanks for reaching out" in _sms_body(client.get(f"/leads/{lead_id}").text)
    assert admin_conn.execute("SELECT count(*) AS n FROM audit WHERE action = 'first_text_updated'").fetchone()["n"] == 2


def test_members_cannot_edit_the_wording_and_other_settings_survive(client, make_tenant, make_user, admin_conn):
    t = make_tenant("ital", name="Ital Paving", twilio_number="+12495550100")
    admin_conn.execute("UPDATE tenant SET settings = settings || %s WHERE id = %s", (Jsonb({"lead_messaging": {"ack": False, "templates": {"ack": "Custom ack {first_name}"}}, "sms": {"relay_replies": False}}), t["id"]))
    make_user("pat@ital.test", tenant_id=t["id"], role="member", name="Pat Member")
    client.post("/login", data={"email": "pat@ital.test", "password": "correct-horse-battery"})
    assert client.get("/messages").status_code == 403
    assert 'href="/messages"' not in client.get("/account").text

    fresh = client.__class__(client.app, base_url="http://testserver")
    with fresh:
        _owner_login(fresh, make_user, admin_conn, t["id"])
        csrf = csrf_from(fresh)
        r = fresh.post("/messages", data={"template": "Hi {first_name}, new wording.", "csrf_token": csrf}, follow_redirects=False)
        assert r.status_code == 303
    s = admin_conn.execute("SELECT settings FROM tenant WHERE id = %s", (t["id"],)).fetchone()["settings"]
    assert s["lead_messaging"]["templates"] == {"ack": "Custom ack {first_name}", "intro": "Hi {first_name}, new wording."}
    assert s["lead_messaging"]["ack"] is False and s["sms"] == {"relay_replies": False}


def test_email_button_offers_a_choice_then_remembers_it(client, make_tenant, make_user, admin_conn):
    from urllib.parse import parse_qs, urlparse

    t = make_tenant("ital", name="Ital Paving", twilio_number="+12495550100")
    make_user("jb@ital.test", tenant_id=t["id"], role="member", name="Jeffrey Braun", mobile="+17055550199")
    lead_id = _lead(client, t, email="karen@example.com")
    client.post("/login", data={"email": "jb@ital.test", "password": "correct-horse-battery"})
    html = client.get(f"/leads/{lead_id}").text
    # No preference yet: the button is a menu with the three ways to open the email.
    assert '<details class="act-menu">' in html and "Gmail in the browser" in html and "Outlook on the web" in html and "Mail app on this device" in html
    gmail = re.search(r'href="(https://mail\.google\.com/mail/\?[^"]+)"', html).group(1).replace("&amp;", "&")
    q = parse_qs(urlparse(gmail).query)
    assert q["to"] == ["karen@example.com"] and q["su"] == ["Ital Paving: your driveway paving"]
    assert q["body"][0].startswith("Hi Karen, thanks for reaching out about your driveway paving.")
    assert "outlook.office.com/mail/deeplink/compose?to=karen%40example.com" in html
    assert 'href="mailto:karen@example.com?subject=Ital%20Paving' in html

    # "Always use Gmail" from the menu saves the preference and comes straight back to the lead.
    csrf = csrf_from(client)
    r = client.post("/account/email-client", data={"email_client": "gmail", "next": f"/leads/{lead_id}", "csrf_token": csrf}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/leads/{lead_id}"
    assert admin_conn.execute("SELECT email_client FROM app_user WHERE email = 'jb@ital.test'").fetchone()["email_client"] == "gmail"
    html = client.get(f"/leads/{lead_id}").text
    assert '<details class="act-menu">' not in html
    assert re.search(r'<a class="act" href="https://mail\.google\.com/[^"]+" target="_blank" rel="noopener" data-touch="email">', html)
    assert "Gmail · karen@example.com" in html
    # The Account page shows the choice and an unknown value falls back to asking.
    assert 'value="gmail" checked' in client.get("/account").text
    client.post("/account/email-client", data={"email_client": "pigeon", "csrf_token": csrf}, follow_redirects=False)
    assert admin_conn.execute("SELECT email_client FROM app_user WHERE email = 'jb@ital.test'").fetchone()["email_client"] == "ask"

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

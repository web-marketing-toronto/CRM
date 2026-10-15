"""Messages to the lead: acknowledgement, held out-of-area decline, appointment confirmation, texting from the app."""

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.db import system_db
from app.models import AreaStatus
from app.services import followup, lead_messages
from tests.conftest import csrf_from

TOR = ZoneInfo("America/Toronto")


def _url(t):
    return f"/v1/intake/{t['slug']}/{t['intake_key']}"


@pytest.fixture
def setup(make_tenant, make_user, admin_conn):
    t = make_tenant("roofco", name="North Ridge Roofing", twilio_number="+17055550000", service_area={"fsas": ["P1L"]},
                    business_hours={"start": "00:00", "end": "23:59", "days": [1, 2, 3, 4, 5, 6, 7]})
    owner = make_user("dan@roofco.test", tenant_id=t["id"], role="owner", name="Dan Owner", mobile="+17055551000")
    member = make_user("pat@roofco.test", tenant_id=t["id"], role="member", name="Pat Member")
    admin_conn.execute("UPDATE tenant SET default_assignee_id = %s WHERE id = %s", (owner["id"], t["id"]))
    t = admin_conn.execute("SELECT * FROM tenant WHERE id = %s", (t["id"],)).fetchone()
    return {"tenant": t, "owner": owner, "member": member}


def _lead_sms(outbox):
    return [m for m in outbox.sms if m["from"] == "+17055550000"]


def test_form_lead_gets_acknowledgement_from_the_clients_number(client, setup, admin_conn, outbox):
    t = setup["tenant"]
    r = client.post(_url(t), json={"name": "Mike Reynolds", "phone": "705 555 0142", "job_type": "Roof replacement", "job_address": "12 Birch Lane, Bracebridge, ON P1L 1A1"})
    lead_id = r.json()["lead_id"]
    acks = _lead_sms(outbox)
    assert len(acks) == 1 and acks[0]["to"] == "+17055550142"
    assert acks[0]["body"] == "Thanks Mike, this is Dan at North Ridge Roofing. Got your request for a roof replacement in Bracebridge, I'll call you shortly. Reply STOP to opt out."
    act = admin_conn.execute("SELECT * FROM activity WHERE lead_id = %s AND type = 'auto_message'", (lead_id,)).fetchone()
    assert act["detail"]["kind"] == "ack" and act["detail"]["delivered"] is True
    # The client alert still went to the owner, from the agency number.
    assert any(m["to"] == "+17055551000" and "New lead" in m["body"] for m in outbox.sms)


def test_ack_falls_back_to_email_and_skips_calls(client, setup, admin_conn, outbox):
    t = setup["tenant"]
    client.post(_url(t), json={"name": "Email Only", "email": "eo@example.com", "job_type": "Repair"})
    emails = [m for m in outbox.email if m["kind"] == "ack"]
    assert len(emails) == 1 and emails[0]["to"] == "eo@example.com" and emails[0]["reply_to"] == "dan@roofco.test" and emails[0]["from_name"] == "North Ridge Roofing"
    assert "Thanks Email, this is Dan" in emails[0]["text"]
    outbox.clear()
    client.post(_url(t), json={"name": "Caller", "phone": "7055550199", "channel": "call", "duration": 45})
    assert not _lead_sms(outbox)
    # Meta instant forms only with the consent switch on.
    client.post(_url(t), json={"name": "Meta Person", "phone": "7055550188", "channel": "meta_lead_ad"})
    assert not _lead_sms(outbox)


def test_no_texting_number_means_no_ack_and_no_noise(client, make_tenant, make_user, admin_conn, outbox):
    t = make_tenant("plain")
    make_user("o@plain.test", tenant_id=t["id"], role="owner", mobile="+17055552000")
    lead_id = client.post(_url(t), json={"name": "Mike", "phone": "7055550142"}).json()["lead_id"]
    assert admin_conn.execute("SELECT count(*) AS n FROM activity WHERE lead_id = %s AND type = 'auto_message'", (lead_id,)).fetchone()["n"] == 0


def test_clearly_out_of_area_form_lead_is_held_then_declined(client, setup, admin_conn, outbox, monkeypatch):
    t = setup["tenant"]
    from app.services import leads as svc

    monkeypatch.setattr(svc, "evaluate_area", lambda tenant, fields, maps_api_key: (AreaStatus.OUT, 43.65, -79.38, True))
    r = client.post(_url(t), json={"name": "Far Away", "phone": "4165550142", "job_address": "12 Main St, Toronto, ON M5V 1A1"})
    lead_id = r.json()["lead_id"]
    assert not _lead_sms(outbox)  # no acknowledgement for a lead we are about to decline
    alert = next(m for m in outbox.sms if m["to"] == "+17055551000")
    assert "OUTSIDE SERVICE AREA" in alert["body"] and "/act/" in alert["body"]
    pending = admin_conn.execute("SELECT rule, due_at FROM reminder WHERE lead_id = %s AND rule = 'decline' AND sent_at IS NULL", (lead_id,)).fetchone()
    assert pending and pending["due_at"] > datetime.now(UTC) + timedelta(minutes=14)
    outbox.clear()
    with system_db() as conn:
        followup.dispatch_due(conn, now=datetime.now(UTC) + timedelta(minutes=16))
    decl = _lead_sms(outbox)
    assert len(decl) == 1 and decl[0]["body"] == "Thanks for reaching out. Unfortunately 12 Main St, Toronto, ON M5V 1A1 is outside the area we serve, so we can't help with this one. Sorry to miss it."
    lead = admin_conn.execute("SELECT status, lost_reason, flags FROM lead WHERE id = %s", (lead_id,)).fetchone()
    assert lead["status"] == "lost" and lead["lost_reason"] == "out_of_area" and "auto_decline_at" not in lead["flags"]
    assert admin_conn.execute("SELECT count(*) AS n FROM reminder WHERE lead_id = %s AND sent_at IS NULL AND cancelled_at IS NULL", (lead_id,)).fetchone()["n"] == 0


def test_cancel_link_in_the_alert_keeps_the_lead_and_acknowledges(client, setup, admin_conn, outbox, monkeypatch):
    t = setup["tenant"]
    from app.services import leads as svc

    monkeypatch.setattr(svc, "evaluate_area", lambda tenant, fields, maps_api_key: (AreaStatus.OUT, 43.65, -79.38, True))
    lead_id = client.post(_url(t), json={"name": "Cottage Owner", "phone": "4165550142", "job_address": "12 Main St, Toronto, ON"}).json()["lead_id"]
    alert = next(m for m in outbox.sms if m["to"] == "+17055551000")
    link = re.search(r"http://testserver(/act/\S+)", alert["body"]).group(1)
    outbox.clear()
    r = client.post(link)
    assert r.status_code == 200 and "sending them the acknowledgement" in r.text
    lead = admin_conn.execute("SELECT status, area_status, flags FROM lead WHERE id = %s", (lead_id,)).fetchone()
    assert lead["status"] == "new" and lead["area_status"] == "in" and lead["flags"] == {}
    assert admin_conn.execute("SELECT count(*) AS n FROM reminder WHERE lead_id = %s AND rule = 'decline' AND cancelled_at IS NOT NULL", (lead_id,)).fetchone()["n"] == 1
    with system_db() as conn:
        followup.dispatch_due(conn, lead_id=lead_id)
    assert any("Thanks Cottage, this is Dan" in m["body"] for m in _lead_sms(outbox))
    # Nothing more goes out at the old decline time.
    outbox.clear()
    with system_db() as conn:
        followup.dispatch_due(conn, now=datetime.now(UTC) + timedelta(minutes=20))
    assert not [m for m in _lead_sms(outbox) if "outside the area" in m["body"]]


def test_possibly_out_of_area_waits_for_a_tap(client, setup, login, admin_conn, outbox):
    t = setup["tenant"]
    lead_id = client.post(_url(t), json={"name": "Maybe", "phone": "7055550142", "job_address": "9 Pine St, Toronto ON M4E 1A1"}).json()["lead_id"]
    assert not _lead_sms(outbox)
    alert = next(m for m in outbox.sms if m["to"] == "+17055551000")
    assert "POSSIBLY OUT OF AREA" in alert["body"]
    login("pat@roofco.test")
    csrf = csrf_from(client)
    page = client.get(f"/leads/{lead_id}")
    assert "Possibly outside the service area" in page.text and "Decline: out of area" in page.text
    outbox.clear()
    r = client.post(f"/leads/{lead_id}/area", data={"decision": "decline", "csrf_token": csrf}, headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert any("outside the area we serve" in m["body"] for m in _lead_sms(outbox))
    lead = admin_conn.execute("SELECT status, lost_reason FROM lead WHERE id = %s", (lead_id,)).fetchone()
    assert lead["status"] == "lost" and lead["lost_reason"] == "out_of_area"
    assert "Automatic text to them" in client.get(f"/leads/{lead_id}").text


def test_marking_lost_out_of_area_can_send_the_decline(client, setup, login, admin_conn, outbox):
    t = setup["tenant"]
    lead_id = client.post(_url(t), json={"name": "Mike", "phone": "7055550142"}).json()["lead_id"]
    login("pat@roofco.test")
    csrf = csrf_from(client)
    outbox.clear()
    client.post(f"/leads/{lead_id}/status", data={"status": "lost", "lost_reason": "out_of_area", "send_decline": "on", "csrf_token": csrf}, headers={"HX-Request": "true"})
    assert any("outside the area we serve" in m["body"] for m in _lead_sms(outbox))
    outbox.clear()
    lead2 = client.post(_url(t), json={"name": "Other", "phone": "7055550143"}).json()["lead_id"]
    outbox.clear()
    client.post(f"/leads/{lead2}/status", data={"status": "lost", "lost_reason": "out_of_area", "csrf_token": csrf}, headers={"HX-Request": "true"})
    assert not _lead_sms(outbox)


def test_appointment_confirms_to_the_lead_and_pauses_reminders(client, setup, login, admin_conn, outbox):
    t = setup["tenant"]
    lead_id = client.post(_url(t), json={"name": "Mike Reynolds", "phone": "7055550142", "job_type": "Roof replacement"}).json()["lead_id"]
    login("pat@roofco.test")
    csrf = csrf_from(client)
    when = (datetime.now(TOR) + timedelta(days=5)).replace(hour=14, minute=30, second=0, microsecond=0)
    outbox.clear()
    r = client.post(f"/leads/{lead_id}/appointment", data={"when": when.strftime("%Y-%m-%dT%H:%M"), "csrf_token": csrf}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and when.strftime("%a %b %-d, %-I:%M %p") in r.text
    confirm = _lead_sms(outbox)
    assert len(confirm) == 1
    # Dan is the assignee, so Dan is who's coming, whoever typed the date in.
    assert confirm[0]["body"] == f"Hi Mike, Dan from North Ridge Roofing will be there {when.strftime('%A, %b %-d')} at 2:30 PM to look at your roof replacement. If you need to reschedule, reply to this text or call (705) 555-0000."
    lead = admin_conn.execute("SELECT status, appointment_at FROM lead WHERE id = %s", (lead_id,)).fetchone()
    assert lead["status"] == "contacted" and lead["appointment_at"] == when.astimezone(UTC)
    rules = [r["rule"] for r in admin_conn.execute("SELECT rule FROM reminder WHERE lead_id = %s AND sent_at IS NULL AND cancelled_at IS NULL ORDER BY due_at", (lead_id,))]
    assert rules[0] == "after_appointment" and rules.count("cadence") == 4 and "nudge" not in rules
    outbox.clear()
    with system_db() as conn:
        followup.dispatch_due(conn, now=when.astimezone(UTC) + timedelta(hours=4))
    assert any("How did the appointment with Mike Reynolds" in m["body"] for m in outbox.sms)
    # Clearing it.
    r = client.post(f"/leads/{lead_id}/appointment", data={"when": "", "csrf_token": csrf}, headers={"HX-Request": "true"})
    assert admin_conn.execute("SELECT appointment_at FROM lead WHERE id = %s", (lead_id,)).fetchone()["appointment_at"] is None


def test_text_from_the_app_goes_through_twilio_and_counts_as_a_touch(client, setup, login, admin_conn, outbox):
    t = setup["tenant"]
    lead_id = client.post(_url(t), json={"name": "Mike", "phone": "7055550142"}).json()["lead_id"]
    login("pat@roofco.test")
    csrf = csrf_from(client)
    outbox.clear()
    r = client.post(f"/leads/{lead_id}/text", data={"body": "Hi Mike, Pat here from North Ridge. When's a good time to come by?", "csrf_token": csrf}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and "Texted from the app" in r.text and "When&#39;s a good time" in r.text
    sent = _lead_sms(outbox)
    assert len(sent) == 1 and sent[0]["to"] == "+17055550142" and sent[0]["kind"] == "lead_text"
    lead = admin_conn.execute("SELECT status, last_touch_at FROM lead WHERE id = %s", (lead_id,)).fetchone()
    assert lead["status"] == "contacted" and lead["last_touch_at"] is not None
    # Cadence restarted off the touch, not the arrival.
    assert admin_conn.execute("SELECT count(*) AS n FROM reminder WHERE lead_id = %s AND rule = 'cadence' AND sent_at IS NULL AND cancelled_at IS NULL", (lead_id,)).fetchone()["n"] == 4
    # Empty body is refused, STOP'd number is refused.
    r = client.post(f"/leads/{lead_id}/text", data={"body": "  ", "csrf_token": csrf}, headers={"HX-Request": "true"})
    assert r.status_code == 400 and "Type the message first" in r.text
    admin_conn.execute("UPDATE lead SET sms_opt_out = true WHERE id = %s", (lead_id,))
    r = client.post(f"/leads/{lead_id}/text", data={"body": "hello", "csrf_token": csrf}, headers={"HX-Request": "true"})
    assert r.status_code == 400 and "texted STOP" in r.text


def test_text_box_needs_a_client_number(client, make_tenant, make_user, login, admin_conn):
    t = make_tenant("plain")
    make_user("m@plain.test", tenant_id=t["id"])
    lead_id = client.post(_url(t), json={"name": "Mike", "phone": "7055550142"}).json()["lead_id"]
    login("m@plain.test")
    page = client.get(f"/leads/{lead_id}")
    assert "Texting from the app needs a texting number" in page.text and 'action="/leads/' + lead_id + '/text"' not in page.text


def test_custom_template_and_placeholders(setup, admin_conn):
    t = setup["tenant"]
    admin_conn.execute("UPDATE tenant SET settings = %s WHERE id = %s", ('{"lead_messaging": {"templates": {"ack": "Hey {first_name}! {client_name} here, {assignee_first_name} will text you about {job} soon."}}}', t["id"]))
    tenant = admin_conn.execute("SELECT * FROM tenant WHERE id = %s", (t["id"],)).fetchone()
    cfg = lead_messages.settings_for(tenant)
    with system_db() as conn:
        ctx = lead_messages.context(conn, tenant, {"id": None, "contact_name": "Sara Lee", "job_type": "Deck repair", "job_address": "1 Lake Rd, Gravenhurst, ON", "assigned_to": None})
    assert lead_messages.render(cfg["templates"]["ack"], ctx) == "Hey Sara! North Ridge Roofing here, Dan will text you about your deck repair soon."
    assert cfg["templates"]["decline"] == lead_messages.TEMPLATES["decline"]  # untouched ones keep the default
    # Missing name reads naturally.
    with system_db() as conn:
        ctx = lead_messages.context(conn, tenant, {"id": None, "contact_name": None, "job_type": None, "job_address": None, "assigned_to": None})
    assert lead_messages.render(lead_messages.TEMPLATES["ack"], ctx).startswith("Thanks, this is Dan at North Ridge Roofing. Got your request, I'll call")

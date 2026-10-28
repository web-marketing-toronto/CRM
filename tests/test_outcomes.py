"""Outcomes and the next step: what happened on a touch, what's planned, and the tap link in texts."""

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.auth import crypto
from app.db import system_db
from app.services import followup
from tests.conftest import csrf_from

TOR = ZoneInfo("America/Toronto")


@pytest.fixture
def world(client, make_tenant, make_user, admin_conn):
    t = make_tenant("roofco", name="North Ridge Roofing", twilio_number="+17055550000", business_hours={"start": "00:00", "end": "23:59", "days": [1, 2, 3, 4, 5, 6, 7]})
    dan = make_user("dan@roofco.test", tenant_id=t["id"], role="member", name="Dan Mercer", mobile="+17055551000")
    admin_conn.execute("UPDATE tenant SET default_assignee_id = %s WHERE id = %s", (dan["id"], t["id"]))
    t = admin_conn.execute("SELECT * FROM tenant WHERE id = %s", (t["id"],)).fetchone()
    return {"tenant": t, "dan": dan}


def _lead(client, t, **extra):
    return client.post(f"/v1/intake/{t['slug']}/{t['intake_key']}", json={"name": "Karen Whitfield", "phone": "7055550142", "job_type": "Roof replacement", "job_address": "14 Birch Lane, Bracebridge", **extra}).json()["lead_id"]


def _rules(admin_conn, lead_id):
    return [r["rule"] for r in admin_conn.execute("SELECT rule FROM reminder WHERE lead_id = %s AND sent_at IS NULL AND cancelled_at IS NULL ORDER BY due_at", (lead_id,))]


def _lead_row(admin_conn, lead_id):
    return admin_conn.execute("SELECT * FROM lead WHERE id = %s", (lead_id,)).fetchone()


def test_spoke_requires_a_next_step_and_the_step_drives_needs_action(client, world, login, admin_conn, outbox):
    t = world["tenant"]
    lead_id = _lead(client, t)
    login("dan@roofco.test")
    csrf = csrf_from(client)
    # Tap Call (logs the attempt), then "Spoke to them": contacted, with a provisional call-back and a prompt for what's next.
    client.post(f"/leads/{lead_id}/touch", data={"type": "call", "csrf_token": csrf}, headers={"HX-Request": "true"})
    r = client.post(f"/leads/{lead_id}/outcome", data={"outcome": "spoke", "csrf_token": csrf}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and "Spoke to Karen.</strong> What's next?" in r.text
    row = _lead_row(admin_conn, lead_id)
    assert row["status"] == "contacted" and row["next_step"] == "call_back" and row["next_step_at"] > datetime.now(UTC)
    assert _rules(admin_conn, lead_id) == ["next_step"]  # no generic cadence while a step is planned
    # Pick the real next step: send a quote in 2 days, with a note.
    r = client.post(f"/leads/{lead_id}/next-step", data={"step": "send_quote", "when": "in_2_days", "note": "wants it by Friday", "csrf_token": csrf}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and "Next:</strong> send a quote" in r.text and "wants it by Friday" in r.text
    row = _lead_row(admin_conn, lead_id)
    assert row["next_step"] == "send_quote" and row["next_step_note"] == "wants it by Friday"
    # Not in Needs action while the step is in the future; in it once the step is due.
    assert "Karen Whitfield" not in client.get("/leads?view=needs_action").text
    assert "Next: send a quote" in client.get("/leads?view=open").text
    admin_conn.execute("UPDATE lead SET next_step_at = now() - interval '1 hour' WHERE id = %s", (lead_id,))
    html = client.get("/leads?view=needs_action").text
    assert "Karen Whitfield" in html and 'class="next due"' in html
    # The reminder for the step says what was planned and carries the tap link; the usual cadence resumes after it.
    admin_conn.execute("UPDATE reminder SET due_at = now() - interval '1 hour' WHERE lead_id = %s AND rule = 'next_step'", (lead_id,))
    outbox.clear()
    with system_db() as conn:
        followup.dispatch_due(conn)
    sms = [m for m in outbox.sms if m["to"] == "+17055551000"]
    assert len(sms) == 1 and sms[0]["body"].startswith("Time to send Karen the quote: Karen Whitfield (A001)") and "(wants it by Friday)" in sms[0]["body"] and "/go/" in sms[0]["body"]
    assert _rules(admin_conn, lead_id).count("cadence") == 4


def test_voicemail_schedules_a_retry_and_a_touch_consumes_the_call_back(client, world, login, admin_conn):
    t = world["tenant"]
    lead_id = _lead(client, t)
    login("dan@roofco.test")
    csrf = csrf_from(client)
    client.post(f"/leads/{lead_id}/touch", data={"type": "call", "csrf_token": csrf}, headers={"HX-Request": "true"})
    r = client.post(f"/leads/{lead_id}/outcome", data={"outcome": "voicemail", "csrf_token": csrf}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and "Next:</strong> call back" in r.text
    row = _lead_row(admin_conn, lead_id)
    assert row["status"] == "no_response" and row["next_step"] == "call_back"
    assert admin_conn.execute("SELECT body FROM activity WHERE lead_id = %s AND type = 'outcome'", (lead_id,)).fetchone()["body"] == "Left a voicemail"
    # Calling again is the planned call-back happening: the step clears and the outcome prompt comes back.
    r = client.post(f"/leads/{lead_id}/touch", data={"type": "call", "csrf_token": csrf}, headers={"HX-Request": "true"})
    assert "How did it go?" in r.text and _lead_row(admin_conn, lead_id)["next_step"] is None


def test_booked_follows_the_clients_appointment_kind(client, world, login, admin_conn, outbox):
    t = world["tenant"]
    login("dan@roofco.test")
    csrf = csrf_from(client)
    when = (datetime.now(TOR) + timedelta(days=3)).replace(hour=14, minute=30, second=0, microsecond=0)
    # Default: site visits.
    lead_id = _lead(client, t)
    html = client.get(f"/leads/{lead_id}").text
    assert "Booked a site visit" in html and "Site visit" in html
    outbox.clear()
    r = client.post(f"/leads/{lead_id}/outcome", data={"outcome": "booked", "when": when.strftime("%Y-%m-%dT%H:%M"), "csrf_token": csrf}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and "Next:</strong> site visit" in r.text
    confirm = [m for m in outbox.sms if m["to"] == "+17055550142"]
    assert len(confirm) == 1 and "Dan from North Ridge Roofing will be there" in confirm[0]["body"] and "to look at your roof replacement" in confirm[0]["body"]
    row = _lead_row(admin_conn, lead_id)
    assert row["status"] == "contacted" and row["next_step"] == "appointment" and row["appointment_at"] == when.astimezone(UTC)
    # A client that books calls instead.
    admin_conn.execute("""UPDATE tenant SET settings = settings || '{"appointments": {"kind": "call"}}' WHERE id = %s""", (t["id"],))
    lead2 = client.post(f"/v1/intake/{t['slug']}/{t['intake_key']}", json={"name": "Priya N", "phone": "7055550143", "job_type": "Eavestrough"}).json()["lead_id"]
    html = client.get(f"/leads/{lead2}").text
    assert "Booked a call" in html and "Scheduled call" in html and "Booked a site visit" not in html
    outbox.clear()
    client.post(f"/leads/{lead2}/outcome", data={"outcome": "booked", "when": when.strftime("%Y-%m-%dT%H:%M"), "csrf_token": csrf}, headers={"HX-Request": "true"})
    confirm = [m for m in outbox.sms if m["to"] == "+17055550143"]
    assert len(confirm) == 1 and "Dan from North Ridge Roofing will call you" in confirm[0]["body"] and "about your eavestrough" in confirm[0]["body"]
    outbox.clear()
    with system_db() as conn:
        followup.dispatch_due(conn, now=when.astimezone(UTC) + timedelta(hours=4))
    assert any(m["body"].startswith("How did the call go? Priya N") for m in outbox.sms)
    # And a client with appointments off has no booking option at all.
    admin_conn.execute("""UPDATE tenant SET settings = settings || '{"appointments": {"kind": "off"}}' WHERE id = %s""", (t["id"],))
    lead3 = client.post(f"/v1/intake/{t['slug']}/{t['intake_key']}", json={"name": "Tom B", "phone": "7055550144"}).json()["lead_id"]
    html = client.get(f"/leads/{lead3}").text
    assert "Booked" not in html and 'value="appointment"' not in html
    r = client.post(f"/leads/{lead3}/outcome", data={"outcome": "booked", "when": when.strftime("%Y-%m-%dT%H:%M"), "csrf_token": csrf}, headers={"HX-Request": "true"})
    assert r.status_code == 400 and "turned off" in r.text


def test_quoted_not_interested_and_spam(client, world, login, admin_conn):
    t = world["tenant"]
    login("dan@roofco.test")
    csrf = csrf_from(client)
    lead_id = _lead(client, t)
    r = client.post(f"/leads/{lead_id}/outcome", data={"outcome": "quoted", "value": "8,500", "when": "in_2_days", "csrf_token": csrf}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and "Next:</strong> call back" in r.text and "follow up on the quote" in r.text
    row = _lead_row(admin_conn, lead_id)
    assert row["status"] == "quoted" and row["next_step"] == "call_back"
    r = client.post(f"/leads/{lead_id}/outcome", data={"outcome": "not_interested", "lost_reason": "price", "csrf_token": csrf}, headers={"HX-Request": "true"})
    row = _lead_row(admin_conn, lead_id)
    assert r.status_code == 200 and row["status"] == "lost" and row["lost_reason"] == "price" and row["next_step"] is None
    assert _rules(admin_conn, lead_id) == []
    lead2 = client.post(f"/v1/intake/{t['slug']}/{t['intake_key']}", json={"name": "Spammy", "phone": "7055550199"}).json()["lead_id"]
    client.post(f"/leads/{lead2}/outcome", data={"outcome": "spam", "csrf_token": csrf}, headers={"HX-Request": "true"})
    assert _lead_row(admin_conn, lead2)["lost_reason"] == "spam"


def test_lead_reply_supersedes_the_planned_step(client, world, login, admin_conn):
    t = world["tenant"]
    login("dan@roofco.test")
    csrf = csrf_from(client)
    lead_id = _lead(client, t)
    client.post(f"/leads/{lead_id}/next-step", data={"step": "waiting", "when": "next_week", "csrf_token": csrf}, headers={"HX-Request": "true"})
    assert _lead_row(admin_conn, lead_id)["next_step"] == "waiting"
    with system_db() as conn:
        tenant = conn.execute("SELECT * FROM tenant WHERE id = %s", (t["id"],)).fetchone()
        lead = conn.execute("SELECT * FROM lead WHERE id = %s", (lead_id,)).fetchone()
        followup.on_lead_replied(conn, tenant, lead, "Yes, Tuesday works")
    row = _lead_row(admin_conn, lead_id)
    assert row["next_step"] is None and "Karen Whitfield" in client.get("/leads?view=needs_action").text


def test_tap_link_signs_in_on_the_phone_and_opens_the_lead(client, world, admin_conn):
    t, dan = world["tenant"], world["dan"]
    lead_id = _lead(client, t)
    link = followup.open_link(lead_id, dan["id"])
    assert link.startswith("http://testserver/go/") and len(link) < 100
    r = client.get(link.replace("http://testserver", ""), follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/leads/{lead_id}#outcome"
    assert "What happened?" in client.get(f"/leads/{lead_id}").text  # signed in: the lead renders
    assert admin_conn.execute("SELECT count(*) AS n FROM session WHERE user_id = %s AND revoked_at IS NULL", (dan["id"],)).fetchone()["n"] == 1
    # A tampered or expired link does not sign anyone in.
    bad = client.get("/go/" + link.rsplit("/", 1)[1][:-2] + "zz", follow_redirects=False)
    assert bad.status_code == 400 and "expired" in bad.text
    assert crypto.read_short_token(crypto.short_token(lead_id, dan["id"], salt="go", ttl_seconds=-1), salt="go") is None
    assert crypto.read_short_token(crypto.short_token(lead_id, dan["id"], salt="go"), salt="other") is None


def test_free_text_reply_to_an_alert_becomes_a_note(client, world, admin_conn, outbox):
    from tests.test_twilio import _post

    t = world["tenant"]
    lead_id = _lead(client, t)
    assert any("/go/" in m["body"] for m in outbox.sms)
    r = _post(client, {"From": "+17055551000", "To": "+14165550000", "Body": "Left a voicemail, will try again tonight", "MessageSid": "N1"})
    assert "Noted on Karen Whitfield (A001)" in r.text and "/go/" in r.text and "Codes" not in r.text
    assert admin_conn.execute("SELECT body FROM activity WHERE lead_id = %s AND type = 'note'", (lead_id,)).fetchone()["body"] == "Left a voicemail, will try again tonight"
    assert re.search(r"Reply 1|codes", r.text) is None

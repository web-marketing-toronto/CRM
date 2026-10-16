"""The follow-up engine: what gets scheduled, what gets sent, and what a reply does."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.db import system_db
from app.services import followup, worker

TOR = ZoneInfo("America/Toronto")


def _url(tenant):
    return f"/v1/intake/{tenant['slug']}/{tenant['intake_key']}"


def _pending(admin_conn, lead_id):
    return {r["rule"]: r for r in admin_conn.execute("SELECT * FROM reminder WHERE lead_id = %s AND sent_at IS NULL AND cancelled_at IS NULL ORDER BY due_at", (lead_id,))}


def _rules(admin_conn, lead_id, **where):
    return [r["rule"] for r in admin_conn.execute("SELECT rule FROM reminder WHERE lead_id = %s AND sent_at IS NULL AND cancelled_at IS NULL ORDER BY due_at", (lead_id,))]


@pytest.fixture
def setup(make_tenant, make_user, admin_conn):
    # Round-the-clock hours so the day arithmetic in these tests does not depend on the weekday they run on.
    t = make_tenant("roofco", business_hours={"start": "00:00", "end": "23:59", "days": [1, 2, 3, 4, 5, 6, 7]})
    owner = make_user("owner@roofco.test", tenant_id=t["id"], role="owner", name="Dave Owner", mobile="+17055551000")
    admin = make_user("jeff@agency.test", role="agency_admin", name="Jeff", mobile="+14165551234")
    admin_conn.execute("UPDATE tenant SET default_assignee_id = %s WHERE id = %s", (owner["id"], t["id"]))
    t = admin_conn.execute("SELECT * FROM tenant WHERE id = %s", (t["id"],)).fetchone()
    return {"tenant": t, "owner": owner, "admin": admin}


def test_new_lead_alerts_immediately_and_schedules_nudges(client, setup, admin_conn, outbox):
    t = setup["tenant"]
    r = client.post(_url(t), json={"name": "Mike Reynolds", "phone": "705 555 0142", "job_type": "Roof replacement", "job_address": "12 Birch Lane, Bracebridge, ON", "gclid": "x"})
    assert r.status_code == 201
    lead_id = r.json()["lead_id"]
    # The new-lead alert went out in the request's background task: one text and one email to the assignee.
    assert len(outbox.sms) == 1 and outbox.sms[0]["to"] == "+17055551000"
    assert "New lead: Mike Reynolds (A001), Roof replacement in Bracebridge" in outbox.sms[0]["body"]
    assert "Reply 1 called" in outbox.sms[0]["body"]
    assert len(outbox.email) == 1 and outbox.email[0]["to"] == "owner@roofco.test"
    assert "/act/" in outbox.email[0]["html"]  # one-tap action links
    # Still pending: two nudges and the agency escalation.
    pending = _pending(admin_conn, lead_id)
    assert set(pending) == {"nudge", "escalation"}
    nudges = [r for r in admin_conn.execute("SELECT * FROM reminder WHERE lead_id = %s AND rule = 'nudge' ORDER BY due_at", (lead_id,))]
    assert len(nudges) == 2
    assert nudges[1]["due_at"] - nudges[0]["due_at"] >= timedelta(hours=3)
    # Message log has both sends.
    kinds = [m["kind"] for m in admin_conn.execute("SELECT kind FROM message_log WHERE lead_id = %s", (lead_id,))]
    assert kinds == ["alert", "alert"]


def test_nudges_wait_for_business_hours():
    t = {"timezone": "America/Toronto", "business_hours": {"start": "07:00", "end": "19:00", "days": [1, 2, 3, 4, 5, 6]}}
    late = datetime(2026, 10, 16, 23, 30, tzinfo=TOR)  # Friday 11:30 pm
    assert followup.next_business_time(late.astimezone(UTC), t).astimezone(TOR) == datetime(2026, 10, 17, 7, 0, tzinfo=TOR)
    sunday = datetime(2026, 10, 18, 10, 0, tzinfo=TOR)
    assert followup.next_business_time(sunday.astimezone(UTC), t).astimezone(TOR) == datetime(2026, 10, 19, 7, 0, tzinfo=TOR)
    noon = datetime(2026, 10, 14, 12, 0, tzinfo=TOR)
    assert followup.next_business_time(noon.astimezone(UTC), t) == noon.astimezone(UTC)


def test_touch_cancels_nudges_and_starts_cadence(client, setup, admin_conn, outbox):
    t = setup["tenant"]
    lead_id = client.post(_url(t), json={"name": "Mike", "phone": "7055550142"}).json()["lead_id"]
    with system_db() as conn:
        from app.models import ActivityType
        from app.services import leads as svc

        lead = svc.get_lead(conn, lead_id)
        svc.log_touch(conn, lead, ActivityType.CALL_ATTEMPT, actor_id=setup["owner"]["id"], actor_label="Dave")
    rules = _rules(admin_conn, lead_id)
    assert rules == ["cadence", "cadence", "cadence", "cadence"]
    cadence = admin_conn.execute("SELECT due_at, payload FROM reminder WHERE lead_id = %s AND rule = 'cadence' ORDER BY due_at", (lead_id,)).fetchall()
    assert cadence[-1]["payload"]["final"] is True
    assert cadence[0]["due_at"] >= datetime.now(UTC) + timedelta(hours=20)
    # Fast-forward: the day-1 reminder goes out and is logged on the lead.
    outbox.clear()
    with system_db() as conn:
        stats = followup.dispatch_due(conn, now=datetime.now(UTC) + timedelta(days=1, hours=12))
    assert stats.sent == 1 and len(outbox.sms) == 1
    assert "Follow up with Mike (A001)" in outbox.sms[0]["body"] and "5 snooze 3 days" in outbox.sms[0]["body"]
    assert admin_conn.execute("SELECT count(*) AS n FROM activity WHERE lead_id = %s AND type = 'reminder_sent'", (lead_id,)).fetchone()["n"] == 1


def test_won_cancels_everything(client, setup, admin_conn):
    t = setup["tenant"]
    lead_id = client.post(_url(t), json={"name": "Mike", "phone": "7055550142"}).json()["lead_id"]
    with system_db() as conn:
        from decimal import Decimal

        from app.models import LeadStatus
        from app.services import leads as svc

        svc.change_status(conn, svc.get_lead(conn, lead_id), LeadStatus.WON, actor_id=None, actor_label="Dave", deal_value=Decimal("8500"))
    assert _rules(admin_conn, lead_id) == []


def test_quoted_schedules_close_the_loop(client, setup, admin_conn, outbox):
    t = setup["tenant"]
    lead_id = client.post(_url(t), json={"name": "Mike", "phone": "7055550142"}).json()["lead_id"]
    with system_db() as conn:
        from app.models import LeadStatus
        from app.services import leads as svc

        svc.change_status(conn, svc.get_lead(conn, lead_id), LeadStatus.QUOTED, actor_id=None, actor_label="Dave")
    assert _rules(admin_conn, lead_id) == ["close_loop", "close_loop"]
    outbox.clear()
    with system_db() as conn:
        followup.dispatch_due(conn, now=datetime.now(UTC) + timedelta(days=11))
    assert any("Did the quote for Mike (A001) close?" in m["body"] for m in outbox.sms)


def test_untouched_lead_escalates_to_agency_after_24h(client, setup, admin_conn, outbox):
    t = setup["tenant"]
    lead_id = client.post(_url(t), json={"name": "Mike", "phone": "7055550142"}).json()["lead_id"]
    outbox.clear()
    with system_db() as conn:
        stats = followup.dispatch_due(conn, now=datetime.now(UTC) + timedelta(hours=25))
    assert stats.escalations == 1
    to_agency = [m for m in outbox.sms if m["to"] == "+14165551234"]
    assert len(to_agency) == 1 and "Roofco: Mike (A001) untouched for 1 d ago" in to_agency[0]["body"]
    assert admin_conn.execute("SELECT count(*) AS n FROM activity WHERE lead_id = %s AND type = 'escalation'", (lead_id,)).fetchone()["n"] == 1


def test_escalation_is_cancelled_once_touched(client, setup, admin_conn, outbox):
    t = setup["tenant"]
    lead_id = client.post(_url(t), json={"name": "Mike", "phone": "7055550142"}).json()["lead_id"]
    with system_db() as conn:
        from app.models import ActivityType
        from app.services import leads as svc

        svc.log_touch(conn, svc.get_lead(conn, lead_id), ActivityType.TEXT_SENT, actor_id=None, actor_label="Dave")
    outbox.clear()
    with system_db() as conn:
        stats = followup.dispatch_due(conn, now=datetime.now(UTC) + timedelta(hours=25))
    assert stats.escalations == 0 and not [m for m in outbox.sms if m["to"] == "+14165551234"]


def test_two_ignored_cadence_reminders_escalate(client, setup, admin_conn, outbox):
    t = setup["tenant"]
    lead_id = client.post(_url(t), json={"name": "Mike", "phone": "7055550142"}).json()["lead_id"]
    with system_db() as conn:
        from app.models import ActivityType
        from app.services import leads as svc

        svc.log_touch(conn, svc.get_lead(conn, lead_id), ActivityType.CALL_ATTEMPT, actor_id=None, actor_label="Dave")
    with system_db() as conn:
        followup.dispatch_due(conn, now=datetime.now(UTC) + timedelta(days=1, hours=12))
    with system_db() as conn:
        followup.dispatch_due(conn, now=datetime.now(UTC) + timedelta(days=3, hours=12))
    pending = _pending(admin_conn, lead_id)
    assert "escalation" in pending and pending["escalation"]["payload"]["why"] == "ignored"
    outbox.clear()
    with system_db() as conn:
        followup.dispatch_due(conn, now=datetime.now(UTC) + timedelta(days=3, hours=13))
    assert any("2 follow-up reminders ignored" in m["body"] for m in outbox.sms if m["to"] == "+14165551234")


def test_reply_codes_from_the_owner(client, setup, admin_conn, outbox):
    t, owner = setup["tenant"], setup["owner"]
    lead_id = client.post(_url(t), json={"name": "Mike", "phone": "7055550142"}).json()["lead_id"]
    with system_db() as conn:
        out = followup.apply_reply_code(conn, owner, "1 left voicemail")
    assert out.handled and "Logged: called Mike (A001)" in out.reply
    lead = admin_conn.execute("SELECT * FROM lead WHERE id = %s", (lead_id,)).fetchone()
    assert lead["status"] == "contacted" and lead["last_touch_at"] is not None
    assert admin_conn.execute("SELECT body FROM activity WHERE lead_id = %s AND type = 'call_attempt'", (lead_id,)).fetchone()["body"] == "left voicemail"
    with system_db() as conn:
        out = followup.apply_reply_code(conn, owner, "5")
    assert "Snoozed Mike (A001) until" in out.reply
    assert _rules(admin_conn, lead_id) == ["snoozed"]
    with system_db() as conn:
        out = followup.apply_reply_code(conn, owner, "W 8,500")
    assert "marked Won at $8,500" in out.reply
    lead = admin_conn.execute("SELECT status, deal_value FROM lead WHERE id = %s", (lead_id,)).fetchone()
    assert lead["status"] == "won" and lead["deal_value"] == 8500
    assert _rules(admin_conn, lead_id) == []


def test_reply_code_with_ref_picks_that_lead(client, setup, admin_conn):
    t, owner = setup["tenant"], setup["owner"]
    client.post(_url(t), json={"name": "First", "phone": "7055550101"})
    second = client.post(_url(t), json={"name": "Second", "phone": "7055550102"}).json()["lead_id"]
    with system_db() as conn:
        out = followup.apply_reply_code(conn, owner, "A002 L price")
    assert "Second (A002) marked Lost (price)" in out.reply
    assert admin_conn.execute("SELECT status, lost_reason FROM lead WHERE id = %s", (second,)).fetchone() == {"status": "lost", "lost_reason": "price"}
    with system_db() as conn:
        out = followup.apply_reply_code(conn, owner, "Z999 1")
    assert "No lead Z999" in out.reply
    with system_db() as conn:
        out = followup.apply_reply_code(conn, owner, "hello?")
    assert out.reply.startswith("Codes:")


def test_lead_reply_alerts_and_restarts_cadence(client, setup, admin_conn, outbox):
    t = setup["tenant"]
    lead_id = client.post(_url(t), json={"name": "Mike", "phone": "7055550142"}).json()["lead_id"]
    outbox.clear()
    with system_db() as conn:
        lead = conn.execute("SELECT * FROM lead WHERE id = %s", (lead_id,)).fetchone()
        followup.on_lead_replied(conn, t, lead, "Yes still interested, call after 5")
    with system_db() as conn:
        followup.dispatch_due(conn, lead_id=lead_id)
    assert any('Mike (A001) replied: "Yes still interested, call after 5"' in m["body"] for m in outbox.sms)
    rules = _rules(admin_conn, lead_id)
    assert rules[0] == "reply_nudge" and rules.count("cadence") == 4
    assert admin_conn.execute("SELECT count(*) AS n FROM activity WHERE lead_id = %s AND type = 'lead_replied'", (lead_id,)).fetchone()["n"] == 1


def test_imported_leads_get_no_reminders(setup, admin_conn):
    t = setup["tenant"]
    admin_conn.execute(
        "INSERT INTO lead (tenant_id, ref_code, contact_name, channel, platform, flags) VALUES (%s, 'A001', 'Old', 'import', 'unknown', '{\"needs_review\": true, \"imported\": true}')",
        (t["id"],),
    )
    lead = admin_conn.execute("SELECT * FROM lead WHERE tenant_id = %s", (t["id"],)).fetchone()
    with system_db() as conn:
        followup.on_lead_created(conn, t, lead)
    assert _rules(admin_conn, lead["id"]) == []


def test_morning_digest_once_per_day(client, setup, admin_conn, outbox):
    t = setup["tenant"]
    client.post(_url(t), json={"name": "Mike", "phone": "7055550142", "job_type": "Repair"})
    client.post(_url(t), json={"name": "Sara", "phone": "7055550143"})
    outbox.clear()
    at = datetime(2026, 10, 15, 8, 0, tzinfo=TOR).astimezone(UTC)  # Thursday 8 am
    with system_db() as conn:
        n = followup.run_digests(conn, now=at)
    assert n >= 1
    digest = [m for m in outbox.email if m["kind"] == "digest"]
    assert len(digest) == 1 and digest[0]["subject"] == "2 leads need a follow-up today" and "Mike (A001), Repair" in digest[0]["text"]
    # A second tick the same morning sends nothing more.
    with system_db() as conn:
        followup.run_digests(conn, now=at + timedelta(minutes=1))
    assert len([m for m in outbox.email if m["kind"] == "digest"]) == 1
    # Before digest time, nothing.
    admin_conn.execute("DELETE FROM message_log")
    outbox.clear()
    with system_db() as conn:
        followup.run_digests(conn, now=datetime(2026, 10, 15, 6, 0, tzinfo=TOR).astimezone(UTC))
    assert not outbox.email


def test_snooze_from_the_app_and_cron_tick(client, setup, login, make_user, admin_conn, outbox):
    from tests.conftest import csrf_from

    t = setup["tenant"]
    make_user("member@roofco.test", tenant_id=t["id"], role="member", name="Pat Member")
    lead_id = client.post(_url(t), json={"name": "Mike", "phone": "7055550142"}).json()["lead_id"]
    login("member@roofco.test")
    csrf = csrf_from(client)
    r = client.post(f"/leads/{lead_id}/snooze", data={"days": "3", "csrf_token": csrf}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and "Snoozed until" in r.text
    assert _rules(admin_conn, lead_id) == ["snoozed"]
    r = client.post("/internal/cron/tick", headers={"X-Cron-Secret": "test-cron"})
    assert r.status_code == 200 and r.json()["ok"] is True and "sent" in r.json()
    # The tick helper itself with a far-future clock sends the snooze-over reminder.
    outbox.clear()
    result = worker.run_tick(now=datetime.now(UTC) + timedelta(days=4))
    assert result["sent"] >= 1 and any("Snooze over" in m["body"] for m in outbox.sms)


def test_email_action_link_logs_a_touch(client, setup, admin_conn):
    t, owner = setup["tenant"], setup["owner"]
    lead_id = client.post(_url(t), json={"name": "Mike", "phone": "7055550142"}).json()["lead_id"]
    link = followup.action_link(lead_id, owner["id"], "call")
    path = link.replace("http://testserver", "")
    r = client.get(path)
    assert r.status_code == 200 and "Log a call to Mike" in r.text
    assert admin_conn.execute("SELECT status FROM lead WHERE id = %s", (lead_id,)).fetchone()["status"] == "new"  # GET changes nothing
    r = client.post(path)
    assert r.status_code == 200 and "Logged: called Mike" in r.text
    assert admin_conn.execute("SELECT status FROM lead WHERE id = %s", (lead_id,)).fetchone()["status"] == "contacted"
    # Won needs a value.
    won = followup.action_link(lead_id, owner["id"], "won").replace("http://testserver", "")
    assert client.post(won, data={"deal_value": "abc"}).status_code == 400
    assert "marked Won at $9,200" in client.post(won, data={"deal_value": "9200"}).text
    # Tampered token.
    assert client.get(path[:-2] + "xx").status_code == 404


def test_opted_out_number_is_never_texted(setup, admin_conn):
    t = setup["tenant"]
    admin_conn.execute("INSERT INTO sms_opt_out (tenant_id, phone_e164) VALUES (%s, '+17055559999')", (t["id"],))
    from app.services import messaging

    with system_db() as conn:
        res = messaging.send_sms(conn, to="+17055559999", body="hi", kind="lead_text", tenant_id=t["id"])
    assert not res.ok and "opted out" in res.error
    assert admin_conn.execute("SELECT status FROM message_log").fetchone()["status"] == "failed"


def test_client_role_can_write_reminders_under_rls(client, setup, admin_conn):
    """The hooks run inside the client's own transaction (owner/member role), so RLS must allow it."""
    t = setup["tenant"]
    lead_id = client.post(_url(t), json={"name": "Mike", "phone": "7055550142"}).json()["lead_id"]
    from app.db import db
    from app.models import ActivityType
    from app.services import leads as svc

    with db(t["id"], "member") as conn:
        svc.log_touch(conn, svc.get_lead(conn, lead_id), ActivityType.CALL_ATTEMPT, actor_id=None, actor_label="Member")
    assert _rules(admin_conn, lead_id) == ["cadence"] * 4
    # And cannot see another tenant's reminders.
    with db(t["id"], "member") as conn:
        assert conn.execute("SELECT count(*) AS n FROM reminder WHERE tenant_id <> %s", (t["id"],)).fetchone()["n"] == 0


def test_agency_saves_followup_schedule(client, make_tenant, make_user, admin_conn):
    import re

    import pyotp

    from app.auth import crypto

    t = make_tenant("alpha")
    u = make_user("jeff@agency.test", role="agency_admin", totp_enabled=True)
    secret = crypto.new_totp_secret()
    admin_conn.execute("UPDATE app_user SET totp_secret_enc = %s WHERE id = %s", (crypto.encrypt_secret(secret), u["id"]))
    client.post("/login", data={"email": "jeff@agency.test", "password": "correct-horse-battery"})
    page = client.get("/login/mfa")
    csrf = re.search(r'name="csrf_token" value="([0-9a-f]+)"', page.text).group(1)
    client.post("/login/mfa", data={"code": pyotp.TOTP(secret).now(), "csrf_token": csrf})
    page = client.get(f"/admin/tenants/{t['id']}")
    assert 'name="cadence_days"' in page.text and 'value="1, 3, 7, 14"' in page.text
    csrf = re.search(r'name="csrf_token" value="([0-9a-f]+)"', page.text).group(1)
    form = {
        "csrf_token": csrf, "name": "Alpha", "timezone": "America/Toronto", "field_map": "{}", "service_area": "{}",
        "hours_start": "7:00", "hours_end": "7:00", "hours_days": ["1", "2", "3", "4", "5"], "job_types": "Roofing",
        "nudge_hours": "2, 6", "cadence_days": "2 5 10", "close_loop_days": "", "escalate_after_hours": "48",
        "ignored_before_escalation": "3", "reply_nudge_hours": "2", "digest_time": "8:00", "is_active": "on",
    }
    r = client.post(f"/admin/tenants/{t['id']}", data=form, follow_redirects=False)
    assert r.status_code == 303, r.text
    saved = admin_conn.execute("SELECT settings FROM tenant WHERE id = %s", (t["id"],)).fetchone()["settings"]["followup"]
    assert saved["nudge_hours"] == [2, 6] and saved["cadence_days"] == [2, 5, 10] and saved["close_loop_days"] == [10, 20]
    assert saved["escalate_after_hours"] == 48 and saved["digest_time"] == "08:00" and saved["ignored_before_escalation"] == 3
    # Bad input is explained, not swallowed.
    r = client.post(f"/admin/tenants/{t['id']}", data={**form, "cadence_days": "soon"})
    assert r.status_code == 400 and "in the follow-up schedule" in r.text
    # The new schedule is what the engine uses.
    tenant = admin_conn.execute("SELECT * FROM tenant WHERE id = %s", (t["id"],)).fetchone()
    assert followup.settings_for(tenant)["cadence_days"] == [2, 5, 10]


def test_smtp_provider_builds_a_proper_message(monkeypatch, admin_conn, make_tenant):
    """The SMTP path: STARTTLS, login as the mailbox, From keeps the display name, Reply-To set, text + HTML parts."""
    import smtplib

    from app import config
    from app.services import messaging

    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=0):
            sent["host"], sent["port"] = host, port

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def ehlo(self):
            sent["ehlo"] = True

        def starttls(self):
            sent["tls"] = True

        def login(self, user, password):
            sent["login"] = (user, password)

        def send_message(self, msg):
            sent["msg"] = msg

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    s = config.get_settings()
    monkeypatch.setattr(s, "email_provider", "smtp")
    monkeypatch.setattr(s, "smtp_password", "app-password")
    monkeypatch.setattr(s, "smtp_user", "leads@web-marketing-toronto.com")
    t = make_tenant("roofco")
    from app.db import system_db

    with system_db() as conn:
        r = messaging.send_email(conn, to="mike@example.com", subject="Your request", text="hi there", html="<p>hi there</p>", kind="ack", tenant_id=t["id"], reply_to="dan@roofco.test", from_name="North Ridge Roofing")
    assert r.ok and r.provider == "smtp" and r.provider_id.startswith("<")
    assert sent["host"] == "smtp.gmail.com" and sent["port"] == 587 and sent["tls"] and sent["login"] == ("leads@web-marketing-toronto.com", "app-password")
    msg = sent["msg"]
    assert msg["From"] == "North Ridge Roofing <leads@web-marketing-toronto.com>" and msg["Reply-To"] == "dan@roofco.test" and msg["To"] == "mike@example.com"
    assert msg.is_multipart() and [p.get_content_type() for p in msg.iter_parts()] == ["text/plain", "text/html"]
    assert admin_conn.execute("SELECT status, provider FROM message_log WHERE channel = 'email'").fetchone() == {"status": "sent", "provider": "smtp"}
    # A refused login is a failed send, not a crash.
    def bad_login(self, user, password):
        raise smtplib.SMTPAuthenticationError(535, b"Username and Password not accepted")

    monkeypatch.setattr(FakeSMTP, "login", bad_login)
    with system_db() as conn:
        r = messaging.send_email(conn, to="mike@example.com", subject="x", text="y", html=None, kind="ack", tenant_id=t["id"])
    assert not r.ok and "535" in r.error

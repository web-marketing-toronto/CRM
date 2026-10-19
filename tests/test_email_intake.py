"""Leads by email (the mailbox poller and its parser) and attribution beacons from wmt.js."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

import pytest

from app import config
from app.services import email_intake


@pytest.fixture
def mail_on(monkeypatch):
    s = config.get_settings()
    monkeypatch.setattr(s, "imap_host", "imap.gmail.com")
    monkeypatch.setattr(s, "smtp_password", "app-password")
    monkeypatch.setattr(s, "email_from", "crm@web-marketing-toronto.com")
    yield


def _msg(to: str, subject: str, text: str | None = None, html: str | None = None, *, from_="RealtyNinja <no-reply@realtyninja.com>", mid="<abc@realtyninja.com>") -> bytes:
    m = EmailMessage()
    m["From"] = from_
    m["To"] = "crm@web-marketing-toronto.com"
    m["Delivered-To"] = to
    m["Subject"] = subject
    m["Message-ID"] = mid
    m["Date"] = "Mon, 19 Oct 2026 16:40:00 -0400"
    if text is not None:
        m.set_content(text)
    if html is not None:
        if text is not None:
            m.add_alternative(html, subtype="html")
        else:
            m.set_content(html, subtype="html")
    return bytes(m)


class FakeMailbox:
    def __init__(self, messages: list[bytes]):
        self.messages = {str(i + 1).encode(): raw for i, raw in enumerate(messages)}
        self.seen: set[bytes] = set()
        self.closed = False

    def unseen(self, limit: int = 50):
        return [u for u in self.messages if u not in self.seen][:limit]

    def fetch(self, uid):
        return self.messages[uid]

    def mark_seen(self, uid):
        self.seen.add(uid)

    def close(self):
        self.closed = True


REALTYNINJA_TEXT = """You have a new lead from your website!

Name: Karen Whitfield
Email: karen.w@example.com
Phone: (705) 555-0142
Listing: 1234 Lake Rosseau Rd, Port Carling https://jeffreybraun.ca/listings/1234-lake-rosseau-rd
Message:
Hi, is this property still available? We'd like to see it this weekend.
Thanks!

--
Sent via RealtyNinja. Manage lead notifications at https://realtyninja.com/settings
"""


def test_parse_labelled_plain_text():
    f = email_intake.parse_fields("New lead from your website", REALTYNINJA_TEXT)
    assert f["name"] == "Karen Whitfield" and f["email"] == "karen.w@example.com" and f["phone"] == "(705) 555-0142"
    assert f["message"].startswith("Hi, is this property still available?") and "Thanks!" in f["message"]
    assert "realtyninja.com/settings" not in f["message"]
    assert f["page_url"] == "https://jeffreybraun.ca/listings/1234-lake-rosseau-rd"
    assert f["form_name"] == "New lead from your website"


def test_parse_html_table_and_fallbacks(mail_on):
    html = """<html><body><h2>Contact form</h2>
    <table><tr><td><b>Full name</b></td><td>Dave &amp; Sue Larocque</td></tr>
    <tr><td>E-mail</td><td><a href="mailto:dave@example.com">dave@example.com</a></td></tr>
    <tr><td>Cell</td><td>705-555-0199</td></tr>
    <tr><td>Comments</td><td>Looking for waterfront<br>under $900k</td></tr></table>
    <p style="color:#888">You are receiving this because you are the site owner. <a href="https://x.test/unsubscribe">Unsubscribe</a></p>
    </body></html>"""
    text = email_intake.html_to_text(html)
    f = email_intake.parse_fields("Contact form submission", text)
    assert f["full_name"] == "Dave & Sue Larocque" and f["e_mail"].startswith("dave@example.com") and f["cell"] == "705-555-0199"
    assert "waterfront" in f["comments"] and "under $900k" in f["comments"]

    # Nothing labelled at all: the email and phone are still found, the name comes from the subject.
    f = email_intake.parse_fields("New inquiry from Priya Natarajan via Contact page", "Please call me at 705 555 0101 or email priya@example.com about the listing on Bala Rd.")
    assert f["email"] == "priya@example.com" and f["phone"] == "705 555 0101" and f["name"] == "Priya Natarajan"


def test_poll_makes_leads_ignores_strangers_and_never_repeats(client, make_tenant, make_user, admin_conn, outbox, mail_on, monkeypatch):
    t = make_tenant("braun", name="Jeffrey Braun Realty")
    owner = make_user("jb@braun.test", tenant_id=t["id"], role="owner", name="Jeffrey Braun", mobile="+17055550199")
    admin_conn.execute("UPDATE tenant SET default_assignee_id = %s WHERE id = %s", (owner["id"], t["id"]))
    t = admin_conn.execute("SELECT * FROM tenant WHERE id = %s", (t["id"],)).fetchone()
    addr = email_intake.address_for(t)
    assert addr.startswith("crm+braun.") and addr.endswith("@web-marketing-toronto.com") and len(addr.split(".")[1].split("@")[0]) == 12

    box = FakeMailbox([
        _msg(addr, "New lead from your website", REALTYNINJA_TEXT, mid="<1@rn>"),
        _msg("crm+braun.000000000000@web-marketing-toronto.com", "New lead", REALTYNINJA_TEXT, mid="<2@rn>"),   # wrong token
        _msg("crm@web-marketing-toronto.com", "Your invoice", "Thanks for your business", from_="billing@twilio.com", mid="<3@tw>"),
        _msg(addr, "New lead from your website", "Someone clicked the button but typed nothing.", mid="<4@rn>"),  # nothing to make a lead from
    ])
    res = email_intake.poll(mailbox=box)
    assert (res.looked_at, res.leads, res.ignored, res.rejected, res.errors) == (4, 1, 2, 1, 0)
    assert box.seen == set(box.messages) and box.closed

    lead = admin_conn.execute("SELECT * FROM lead WHERE tenant_id = %s", (t["id"],)).fetchone()
    assert lead["contact_name"] == "Karen Whitfield" and lead["phone_e164"] == "+17055550142" and lead["email"] == "karen.w@example.com"
    assert lead["channel"] == "website_form" and lead["assigned_to"] == owner["id"]
    assert lead["message"].startswith("Hi, is this property still available?")
    src = admin_conn.execute("SELECT * FROM lead_source WHERE lead_id = %s", (lead["id"],)).fetchone()
    assert src["landing_page"] == "https://jeffreybraun.ca/listings/1234-lake-rosseau-rd" and src["form_name"] == "New lead from your website"
    assert src["raw_payload"]["_email"]["from"] == "no-reply@realtyninja.com"
    rows = {r["message_id"]: r for r in admin_conn.execute("SELECT * FROM inbound_email").fetchall()}
    assert rows["<1@rn>"]["status"] == "lead" and rows["<1@rn>"]["lead_id"] == lead["id"]
    assert rows["<2@rn>"]["status"] == "ignored" and rows["<3@tw>"]["status"] == "ignored"
    assert rows["<4@rn>"]["status"] == "rejected" and "needs at least" in rows["<4@rn>"]["error"]

    # The same message delivered again (a re-poll, a second copy) is not a second lead.
    again = FakeMailbox([_msg(addr, "New lead from your website", REALTYNINJA_TEXT, mid="<1@rn>")])
    res = email_intake.poll(mailbox=again)
    assert res.leads == 0 and admin_conn.execute("SELECT count(*) AS n FROM lead").fetchone()["n"] == 1

    # The tick picks it up and the alert goes out, like any other new lead.
    r = client.post("/internal/cron/tick", headers={"X-Cron-Secret": "test-cron"})
    assert r.status_code == 200
    alerts = [m for m in outbox.sms if m["to"] == "+17055550199"]
    assert alerts and "Karen" in alerts[0]["body"]


def test_poll_runs_from_the_tick_and_shows_on_the_dashboard(client, make_tenant, make_user, admin_conn, mail_on, monkeypatch):
    t = make_tenant("braun", name="Jeffrey Braun Realty")
    make_user("jb@braun.test", tenant_id=t["id"], role="owner", mobile="+17055550199")
    monkeypatch.setattr(email_intake, "open_mailbox", lambda: FakeMailbox([_msg(email_intake.address_for(t), "New lead", REALTYNINJA_TEXT, mid="<9@rn>")]))
    r = client.post("/internal/cron/tick", headers={"X-Cron-Secret": "test-cron"})
    assert r.status_code == 200 and r.json()["email_intake"]["leads"] == 1
    assert admin_conn.execute("SELECT count(*) AS n FROM lead").fetchone()["n"] == 1
    assert admin_conn.execute("SELECT finished_at FROM job_run WHERE name = 'email_intake'").fetchone()["finished_at"] is not None

    from tests.test_dashboard import agency_login

    agency_login(client, make_user, admin_conn)
    html = client.get("/dashboard").text
    assert "Leads by email" in html and "reading crm@web-marketing-toronto.com" in html and "1 lead email in 24 h" in html
    page = client.get(f"/admin/tenants/{t['id']}").text
    assert email_intake.address_for(t) in page and "Lead BCC Email" in page and f"wmt.js?c=braun.{t['public_key']}" in page


def test_mailbox_failure_is_reported_not_fatal(client, make_tenant, admin_conn, mail_on, monkeypatch):
    def boom():
        raise OSError("IMAP login failed: [AUTHENTICATIONFAILED] Invalid credentials")

    monkeypatch.setattr(email_intake, "open_mailbox", boom)
    r = client.post("/internal/cron/tick", headers={"X-Cron-Secret": "test-cron"})
    assert r.status_code == 200 and "Invalid credentials" in r.json()["email_intake"]["error"]
    assert "Invalid credentials" in admin_conn.execute("SELECT detail->>'error' AS e FROM job_run WHERE name = 'email_intake'").fetchone()["e"]


# --- Beacons ------------------------------------------------------------------------------------------

def test_beacon_gives_an_emailed_lead_its_click_id(client, make_tenant, make_user, admin_conn, mail_on):
    t = make_tenant("braun", name="Jeffrey Braun Realty")
    make_user("jb@braun.test", tenant_id=t["id"], role="owner", mobile="+17055550199")
    payload = {"email": "Karen.W@example.com", "phone": "705-555-0142", "name": "Karen Whitfield", "gclid": "Cj0KCQtest", "utm_campaign": "Muskoka Waterfront",
               "landing_page": "https://jeffreybraun.ca/?gclid=Cj0KCQtest", "referrer": "https://www.google.com/", "page_url": "https://jeffreybraun.ca/listings/1234"}
    # Sent by navigator.sendBeacon as text/plain, from the client's site.
    r = client.post(f"/v1/beacon/braun/{t['public_key']}", content=json.dumps(payload), headers={"Content-Type": "text/plain", "Origin": "https://jeffreybraun.ca"})
    assert r.status_code == 204 and r.headers["access-control-allow-origin"] == "*"
    assert client.options(f"/v1/beacon/braun/{t['public_key']}").status_code == 204
    # A wrong key is swallowed silently and stores nothing.
    client.post("/v1/beacon/braun/deadbeefdeadbeef", content=json.dumps(payload), headers={"Content-Type": "text/plain"})
    assert admin_conn.execute("SELECT count(*) AS n FROM attribution_beacon").fetchone()["n"] == 1

    # The lead arrives three minutes later by email, with no click id in it.
    box = FakeMailbox([_msg(email_intake.address_for(t), "New lead from your website", REALTYNINJA_TEXT, mid="<b1@rn>")])
    email_intake.poll(mailbox=box)
    lead = admin_conn.execute("SELECT * FROM lead").fetchone()
    src = admin_conn.execute("SELECT * FROM lead_source WHERE lead_id = %s", (lead["id"],)).fetchone()
    assert lead["platform"] == "google_ads" and lead["campaign_name"] == "Muskoka Waterfront"
    assert src["gclid"] == "Cj0KCQtest" and src["utm_campaign"] == "Muskoka Waterfront" and src["referrer"] == "https://www.google.com/"
    assert src["landing_page"] == "https://jeffreybraun.ca/listings/1234-lake-rosseau-rd"  # the email's own page wins; the beacon fills blanks only
    assert admin_conn.execute("SELECT lead_id FROM attribution_beacon").fetchone()["lead_id"] == lead["id"]
    created = admin_conn.execute("SELECT detail FROM activity WHERE lead_id = %s AND type = 'created'", (lead["id"],)).fetchone()["detail"]
    assert created["platform"] == "google_ads" and created["beacon"] is True


def test_beacon_matches_on_phone_and_expires(client, make_tenant, admin_conn, mail_on):
    t = make_tenant("braun")
    client.post(f"/v1/beacon/braun/{t['public_key']}", content=json.dumps({"phone": "(705) 555-0142", "fbclid": "IwAR123"}), headers={"Content-Type": "text/plain"})
    old = client.post(f"/v1/beacon/braun/{t['public_key']}", content=json.dumps({"email": "old@example.com", "gclid": "OLD"}), headers={"Content-Type": "text/plain"})
    assert old.status_code == 204
    admin_conn.execute("UPDATE attribution_beacon SET created_at = %s WHERE email_lower = 'old@example.com'", (datetime.now(UTC) - timedelta(hours=49),))

    r = client.post(f"/v1/intake/{t['slug']}/{t['intake_key']}", json={"name": "Karen", "phone": "705 555 0142"})
    assert r.status_code == 201 and r.json()["platform"] == "meta_ads"
    r = client.post(f"/v1/intake/{t['slug']}/{t['intake_key']}", json={"name": "Old Lead", "email": "old@example.com"})
    assert r.status_code == 201 and r.json()["platform"] == "unknown"
    # A lead that brought its own click id keeps it; the beacon is not consulted.
    client.post(f"/v1/beacon/braun/{t['public_key']}", content=json.dumps({"email": "own@example.com", "gclid": "FROM_BEACON"}), headers={"Content-Type": "text/plain"})
    r = client.post(f"/v1/intake/{t['slug']}/{t['intake_key']}", json={"name": "Own", "email": "own@example.com", "gclid": "FROM_FORM"})
    lid = r.json()["lead_id"]
    assert admin_conn.execute("SELECT gclid FROM lead_source WHERE lead_id = %s", (lid,)).fetchone()["gclid"] == "FROM_FORM"

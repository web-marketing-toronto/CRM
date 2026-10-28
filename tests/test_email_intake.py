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


REALTYNINJA_HTML = """<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" "https://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">
<html xmlns="https://www.w3.org/1999/xhtml" style="margin: 0;padding: 0;">
<head style="margin: 0;padding: 0;">
<meta name="viewport" content="width=device-width" style="margin: 0;padding: 0;">
<meta http-equiv="Content-Type" content="text/html; charset=UTF-8" style="margin: 0;"><meta name="lead_phone" content="7055550142" />
<meta name="lead_message" content="Is the Lake Rosseau place still available?" />
<meta name="lead_email" content="steve@example.com" />
<meta name="lead_name" content="Steve Winwood" />
<meta name="lead_information_version" content="1.0" />
<meta name="lead_source" content="jeffreybraun.ca" />
<title>RealtyNinja Email</title>
</head>
<body bgcolor="#FFFFFF" style="margin: 0;padding: 0;">
<table class="head-wrap" bgcolor="white" style="margin: 0;padding: 0;width: 100%;">
  <tr><td></td>
    <td class="header container"><div class="content"><table style="width: 100%;"><tr><td>
      <a href="https://www.realtyninja.com"><img src="https://www.realtyninja.com/static/images/emails/realtyninja.png"></a>
    </td></tr></table></div></td>
    <td></td></tr>
</table>
<table class="body-wrap" style="width: 100%;">
  <tr><td></td>
    <td class="container" bgcolor="#FFFFFF"><div class="content"><table style="width: 100%;"><tr><td>
          <hr></hr>
          <div style="display:none !important; visibility:hidden; mso-hide:all; font-size:1px; color:#ffffff; line-height:1px; max-height:0px; max-width:0px; opacity:0; overflow:hidden;">
Steve Winwood filled out a contact form on jeffreybraun.ca -
</div>
<h2 style="font-size: 22px;">A user on <a href="https://www.jeffreybraun.ca" style="color: #0faddb;">jeffreybraun.ca</a> filled out the contact form on October 19, 2026 at  5:07pm. Here's the info they provided:</h2>
<table border="0" cellpadding="0" cellspacing="0" width="100%">
  <tr>
    <td align="left" valign="top" width="35%" style="padding-bottom:6px;">
      <strong>Name:</strong>
    </td>
    <td align="left" valign="top" width="65%" style="padding-bottom:6px;">
      Steve Winwood
    </td>
  </tr>
  <tr>
    <td align="left" valign="top" width="35%"><strong>Email:</strong></td>
    <td align="left" valign="top" width="65%">
      <a href="mailto:steve@example.com" style="color: #0faddb; text-decoration: none;">
\t    steve@example.com
\t  </a>
    </td>
  </tr>
  <tr>
    <td align="left" valign="top" width="35%"><strong>Phone:</strong></td>
    <td align="left" valign="top" width="65%">
      7055550142
    </td>
  </tr>
</table>
<p><strong>Message:</strong><br />
  Is the Lake Rosseau place still available?
</p>
  <p><strong>The user agreed to the following disclaimer:</strong><br />
    By clicking &quot;Send message&quot;, you agree this form collects your name, telephone number and email address along with your message so that I can communicate with you and provide you with assistance.
  </p>
<hr></hr>
  <p>They filled in the form on this page:<br />
    <a href="https://www.jeffreybraun.ca/contact" style="color: #0faddb;">https://www.jeffreybraun.ca/contact</a>
  </p>
<hr></hr>
          <p style="font-size:16px; color:#979797;">Does this look spammy? forward it to support@realtyninja.com and we'll update our spam filters.</p>
    </td></tr></table></div></td>
    <td></td></tr>
</table>
<table class="footer-wrap"><tr><td></td><td class="container"><div class="content"><table><tr><td>
  <p class="footer"><a href="https://www.realtyninja.com">RealtyNinja Services Ltd. - Awesome websites for busy real estate agents.</a><br />
  170 - 422 Richards St. Vancouver, B.C. V6B 2Z4 Canada, +1.888.767.7421<br /><br /></p>
</td></tr></table></div></td><td></td></tr></table>
</body></html>"""


def test_realtyninja_email_exactly_as_sent(client, make_tenant, make_user, admin_conn, mail_on):
    """The real RealtyNinja notification: HTML only, lead in <meta> tags, label/value table cells with
    whitespace inside, Reply-To set to the lead, and a Postmark footer."""
    # The flattened layout alone reads correctly (what any other platform's table email gets).
    text = email_intake.html_to_text(REALTYNINJA_HTML)
    assert "Name: Steve Winwood" in text and "Email: steve@example.com" in text and "Phone: 7055550142" in text
    assert "filled out a contact form" not in text  # hidden preheader dropped
    f = email_intake.parse_fields("[RealtyNinja] Contact Form", text)
    assert f["name"] == "Steve Winwood" and f["email"] == "steve@example.com" and f["phone"] == "7055550142"
    assert f["message"] == "Is the Lake Rosseau place still available?"
    assert f["page_url"] == "https://www.jeffreybraun.ca/contact"
    # And the meta tags are read as exact values.
    assert email_intake.meta_fields(REALTYNINJA_HTML) == {"phone": "7055550142", "message": "Is the Lake Rosseau place still available?", "email": "steve@example.com", "name": "Steve Winwood", "source": "jeffreybraun.ca"}

    t = make_tenant("jb", name="Jeffrey Braun Realty")
    owner = make_user("jb@braun.test", tenant_id=t["id"], role="owner", name="Jeffrey Braun", mobile="+17055550199")
    admin_conn.execute("UPDATE tenant SET default_assignee_id = %s WHERE id = %s", (owner["id"], t["id"]))
    t = admin_conn.execute("SELECT * FROM tenant WHERE id = %s", (t["id"],)).fetchone()
    m = EmailMessage()
    m["From"] = "RealtyNinja <notifications@realtyninja.com>"
    m["To"] = "jeffrey@example.com"
    m["Reply-To"] = "steve@example.com"
    m["Delivered-To"] = email_intake.address_for(t)
    m["Subject"] = "[RealtyNinja] Contact Form"
    m["Message-ID"] = "<cfc5fd73@mtasv.net>"
    m["Date"] = "Mon, 19 Oct 2026 21:07:21 +0000"
    m.set_content(REALTYNINJA_HTML, subtype="html", cte="quoted-printable")
    res = email_intake.poll(mailbox=FakeMailbox([bytes(m)]))
    assert res.leads == 1
    lead = admin_conn.execute("SELECT * FROM lead WHERE tenant_id = %s", (t["id"],)).fetchone()
    assert lead["contact_name"] == "Steve Winwood" and lead["email"] == "steve@example.com" and lead["phone_e164"] == "+17055550142"
    assert lead["message"] == "Is the Lake Rosseau place still available?"
    src = admin_conn.execute("SELECT * FROM lead_source WHERE lead_id = %s", (lead["id"],)).fetchone()
    assert src["landing_page"] == "https://www.jeffreybraun.ca/contact" and src["form_name"] == "[RealtyNinja] Contact Form"


def test_reply_to_fills_in_a_missing_email(mail_on, make_tenant, admin_conn):
    t = make_tenant("jb")
    m = EmailMessage()
    m["From"] = "Site <no-reply@somesite.com>"
    m["Reply-To"] = "visitor@example.com"
    m["Delivered-To"] = email_intake.address_for(t)
    m["Subject"] = "New contact"
    m["Message-ID"] = "<r1@somesite>"
    m.set_content("Name: Pat Visitor\nPhone: 705 555 0100\nMessage: hello")
    assert email_intake.poll(mailbox=FakeMailbox([bytes(m)])).leads == 1
    assert admin_conn.execute("SELECT email FROM lead").fetchone()["email"] == "visitor@example.com"


# --- Email conversations on a lead ----------------------------------------------------------------

def _email_world(client, make_tenant, make_user, admin_conn):
    t = make_tenant("roofco", name="North Ridge Roofing", business_hours={"start": "00:00", "end": "23:59", "days": [1, 2, 3, 4, 5, 6, 7]})
    dan = make_user("dan@northridgeroofing.ca", tenant_id=t["id"], role="member", name="Dan Mercer", mobile="+17055551000")
    admin_conn.execute("UPDATE tenant SET default_assignee_id = %s WHERE id = %s", (dan["id"], t["id"]))
    t = admin_conn.execute("SELECT * FROM tenant WHERE id = %s", (t["id"],)).fetchone()
    lead_id = client.post(f"/v1/intake/{t['slug']}/{t['intake_key']}", json={"name": "Karen Whitfield", "email": "karen.w@example.com", "phone": "7055550142", "job_type": "Roof replacement"}).json()["lead_id"]
    lead = admin_conn.execute("SELECT * FROM lead WHERE id = %s", (lead_id,)).fetchone()
    return t, dan, lead


def _raw(to: str, subject: str, text: str, *, from_: str, mid: str, cc: str | None = None) -> bytes:
    m = EmailMessage()
    m["From"] = from_
    m["To"] = to
    if cc:
        m["Cc"] = cc
    m["Delivered-To"] = "crm@web-marketing-toronto.com"
    m["Subject"] = subject
    m["Message-ID"] = mid
    m["Date"] = "Wed, 28 Oct 2026 14:40:00 -0400"
    m.set_content(text)
    return bytes(m)


def test_email_from_the_app_is_tracked_and_the_reply_lands_on_the_lead(client, make_tenant, make_user, admin_conn, outbox, mail_on, login):
    from tests.conftest import csrf_from

    t, dan, lead = _email_world(client, make_tenant, make_user, admin_conn)
    addr = email_intake.lead_address(t, lead)
    assert addr == email_intake.address_for(t).replace("@", ".a001@")
    login("dan@northridgeroofing.ca")
    csrf = csrf_from(client)
    html = client.get(f"/leads/{lead['id']}").text
    assert 'id="email-box"' in html and "their reply shows up below and alerts you" in html
    assert f"bcc={addr.replace('@', '%40').replace('+', '%2B')}" in html  # the Gmail/Outlook/mailto links keep a copy on the lead
    outbox.clear()
    r = client.post(f"/leads/{lead['id']}/email", data={"subject": "", "body": "Hi Karen, happy to come by Thursday. Does 2 pm work?", "csrf_token": csrf}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and "Emailed from the app" in r.text and "Does 2 pm work?" in r.text
    sent = [m for m in outbox.email if m["to"] == "karen.w@example.com"]
    assert len(sent) == 1 and sent[0]["reply_to"] == addr and sent[0]["from_name"] == "Dan at North Ridge Roofing" and sent[0]["subject"] == "North Ridge Roofing: your roof replacement"
    row = admin_conn.execute("SELECT status, last_touch_at, first_contact_at FROM lead WHERE id = %s", (lead["id"],)).fetchone()
    assert row["status"] == "contacted" and row["first_contact_at"] is not None

    # Karen replies to that email: it goes to the lead's address, attaches as a reply, and Dan is alerted.
    outbox.clear()
    box = FakeMailbox([_raw(addr, "Re: North Ridge Roofing: your roof replacement", "Thursday at 2 is perfect, see you then!\n\nOn Wed, Oct 28, 2026 Dan at North Ridge Roofing wrote:\n> Hi Karen, happy to come by Thursday.", from_="Karen Whitfield <karen.w@example.com>", mid="<r1@gmail>")])
    res = email_intake.poll(mailbox=box)
    assert res.replies == 1 and res.leads == 0
    act = admin_conn.execute("SELECT body, detail FROM activity WHERE lead_id = %s AND type = 'lead_replied'", (lead["id"],)).fetchone()
    assert act["body"] == "Thursday at 2 is perfect, see you then!" and act["detail"]["channel"] == "email"
    from app.services import worker

    worker.dispatch_lead_now(lead["id"])
    alert_sms = [m for m in outbox.sms if m["to"] == "+17055551000"]
    assert alert_sms and alert_sms[0]["body"].startswith('Karen Whitfield (A001), Roof replacement replied by email: "Thursday at 2 is perfect') and "/go/" in alert_sms[0]["body"]
    alert_mail = [m for m in outbox.email if m["to"] == "dan@northridgeroofing.ca"]
    assert alert_mail and alert_mail[0]["reply_to"] == addr  # answer from the inbox and it goes through the CRM
    html = client.get(f"/leads/{lead['id']}").text
    assert "They replied by email" in html and "Thursday at 2 is perfect" in html

    # Dan answers that alert from his inbox: only the lead address is in To, so it is relayed on to Karen.
    outbox.clear()
    box = FakeMailbox([_raw(addr, "Re: Karen Whitfield (A001), Roof replacement replied by email", "Great, see you Thursday.\n\nOn Wed, Oct 28 the CRM wrote:\n> Karen Whitfield just replied", from_="Dan Mercer <dan@northridgeroofing.ca>", mid="<r2@gmail>")])
    res = email_intake.poll(mailbox=box)
    assert res.replies == 1
    relayed = [m for m in outbox.email if m["to"] == "karen.w@example.com"]
    assert len(relayed) == 1 and relayed[0]["text"] == "Great, see you Thursday." and relayed[0]["reply_to"] == addr and relayed[0]["from_name"] == "Dan at North Ridge Roofing"
    assert admin_conn.execute("SELECT count(*) AS n FROM activity WHERE lead_id = %s AND type = 'email_sent' AND detail->>'via' = 'inbox'", (lead["id"],)).fetchone()["n"] == 1


def test_bcc_copy_from_a_mail_app_fills_in_the_logged_attempt(client, make_tenant, make_user, admin_conn, outbox, mail_on, login):
    from tests.conftest import csrf_from

    t, dan, lead = _email_world(client, make_tenant, make_user, admin_conn)
    addr = email_intake.lead_address(t, lead)
    login("dan@northridgeroofing.ca")
    csrf = csrf_from(client)
    # Tapping the Email button logs a bare attempt; the BCC copy that arrives a minute later fills it in.
    client.post(f"/leads/{lead['id']}/touch", data={"type": "email", "csrf_token": csrf}, headers={"HX-Request": "true"})
    outbox.clear()
    box = FakeMailbox([_raw("Karen Whitfield <karen.w@example.com>", "North Ridge Roofing: your roof replacement", "Hi Karen, thanks for reaching out. When suits you?", from_="Dan Mercer <dan@northridgeroofing.ca>", mid="<c1@gmail>", cc=None)])
    # The BCC does not show in To/Cc; Delivered-To carries the lead address.
    raw = box.messages[b"1"].replace(b"Delivered-To: crm@web-marketing-toronto.com", f"Delivered-To: {addr}".encode())
    box.messages[b"1"] = raw
    res = email_intake.poll(mailbox=box)
    assert res.replies == 1 and not [m for m in outbox.email if m["to"] == "karen.w@example.com"]  # kept, not re-sent
    rows = admin_conn.execute("SELECT body, detail FROM activity WHERE lead_id = %s AND type = 'email_sent' ORDER BY id", (lead["id"],)).fetchall()
    assert len(rows) == 1 and rows[0]["body"] == "Hi Karen, thanks for reaching out. When suits you?" and rows[0]["detail"]["via"] == "copy"


def test_reply_text_strips_the_quoted_message():
    assert email_intake.reply_text("Yes please\n\nOn Tue, Oct 27, 2026 at 9:00 AM Dan wrote:\n> original") == "Yes please"
    assert email_intake.reply_text("Sure.\n> quoted line\n> more\nthanks") == "Sure.\nthanks"
    assert email_intake.reply_text("Ok\n\nSent from my iPhone") == "Ok"
    assert email_intake.reply_text("-----Original Message-----\nFrom: x") == ""

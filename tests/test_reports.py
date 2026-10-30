"""Reports: the numbers, the pages, the CSV and the monthly PDF."""

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pyotp
import pytest

from app.auth import crypto
from app.db import system_db
from app.models import ActivityType, LeadStatus
from app.services import leads as svc
from app.services import reports


@pytest.fixture
def world(client, make_tenant, make_user, admin_conn):
    """Two clients, a month of leads with outcomes, spend for one of them."""
    t = make_tenant("roofco", name="North Ridge Roofing", settings={"google_ads": {"customer_id": "1234567890"}, "reports": {"show_spend": True}},
                    business_hours={"start": "00:00", "end": "23:59", "days": [1, 2, 3, 4, 5, 6, 7]})
    other = make_tenant("paintco", name="Lakeside Painting")
    member = make_user("pat@roofco.test", tenant_id=t["id"], role="member", name="Pat")
    url = f"/v1/intake/{t['slug']}/{t['intake_key']}"
    ids = {}
    ids["g1"] = client.post(url, json={"name": "G One", "phone": "7055550001", "gclid": "a", "utm_campaign": "Roofing - Muskoka", "campaign_id": "c1"}).json()["lead_id"]
    ids["g2"] = client.post(url, json={"name": "G Two", "phone": "7055550002", "gclid": "b", "campaign_id": "c1"}).json()["lead_id"]
    ids["g3"] = client.post(url, json={"name": "G Three", "phone": "7055550003", "gclid": "c", "campaign_id": "c2", "campaign_name": "Siding"}).json()["lead_id"]
    ids["m1"] = client.post(url, json={"name": "M One", "phone": "7055550004", "fbclid": "x"}).json()["lead_id"]
    ids["o1"] = client.post(url, json={"name": "Organic", "phone": "7055550005", "referrer": "https://www.google.com/"}).json()["lead_id"]
    ids["call"] = client.post(f"/v1/callrail/{t['slug']}/{t['intake_key']}", json={"id": "C1", "answered": True, "duration": 90, "customer_phone_number": "+17055550006", "source": "Direct"}).json()["lead_id"]
    client.post(f"/v1/intake/{other['slug']}/{other['intake_key']}", json={"name": "Other Client Lead", "phone": "7055550007"})
    now = datetime.now(UTC)
    with system_db() as conn:
        # g1: contacted after 20 minutes, won for $8,500
        lead = svc.get_lead(conn, ids["g1"])
        svc.log_touch(conn, lead, ActivityType.CALL_ATTEMPT, actor_id=None, actor_label="Pat", now=lead["created_at"] + timedelta(minutes=20))
        conn.execute("UPDATE lead SET first_contact_at = created_at + interval '20 minutes' WHERE id = %s", (ids["g1"],))
        svc.change_status(conn, svc.get_lead(conn, ids["g1"]), LeadStatus.WON, actor_id=None, actor_label="Pat", deal_value=Decimal("8500"))
        # g2: contacted after 3 hours, lost on price
        conn.execute("UPDATE lead SET first_contact_at = created_at + interval '3 hours', last_touch_at = created_at + interval '3 hours', status = 'contacted' WHERE id = %s", (ids["g2"],))
        g2 = svc.get_lead(conn, ids["g2"])
        svc.log_activity(conn, t["id"], ids["g2"], ActivityType.CALL_ATTEMPT, actor_label="Pat", occurred_at=g2["created_at"] + timedelta(hours=3))
        svc.change_status(conn, svc.get_lead(conn, ids["g2"]), LeadStatus.LOST, actor_id=None, actor_label="Pat", lost_reason=svc.LostReason.PRICE)
        # g3: spam
        svc.change_status(conn, svc.get_lead(conn, ids["g3"]), LeadStatus.LOST, actor_id=None, actor_label="Pat", lost_reason=svc.LostReason.SPAM)
        # m1 untouched and 5 days old (non-compliant), o1 untouched and brand new (fine)
        conn.execute("UPDATE lead SET created_at = created_at - interval '5 days' WHERE id = %s", (ids["m1"],))
    for d, spend in (("c1", 300.0), ("c2", 100.0)):
        admin_conn.execute(
            "INSERT INTO ad_spend (tenant_id, platform, campaign_id, campaign_name, date, spend, clicks, impressions) VALUES (%s, 'google_ads', %s, %s, %s, %s, 10, 100)",
            (t["id"], d, "Roofing - Muskoka" if d == "c1" else "Siding", now.date(), spend),
        )
    admin_conn.execute("INSERT INTO ad_spend (tenant_id, platform, campaign_id, campaign_name, date, spend) VALUES (%s, 'meta_ads', 'mc1', 'Meta Leads', %s, 50)", (t["id"], now.date()))
    return {"tenant": admin_conn.execute("SELECT * FROM tenant WHERE id = %s", (t["id"],)).fetchone(), "other": other, "member": member, "ids": ids}


def test_tenant_summary_numbers(world):
    t = world["tenant"]
    with system_db() as conn:
        s = reports.tenant_summary(conn, t, reports.period("last_30"))
    tot = s.total
    assert tot.leads == 6 and tot.won == 1 and tot.lost == 2 and tot.revenue == 8500.0 and tot.bad == 1
    assert tot.contacted == 3  # g1, g2 and the answered call
    assert tot.spend == 450.0 and tot.cpl == 75.0 and tot.cost_per_won == 450.0
    g = next(r for r in s.by_platform if r.key == "google_ads")
    assert (g.leads, g.won, g.spend, g.cpl, g.cost_per_won, g.bad_share) == (3, 1, 400.0, 133.33, 400.0, 33.3)
    assert g.close_rate == 33.3 and round(g.roas, 2) == 21.25
    m = next(r for r in s.by_platform if r.key == "meta_ads")
    assert (m.leads, m.spend, m.cpl) == (1, 50.0, 50.0)
    assert {r.key for r in s.by_platform} >= {"google_ads", "meta_ads", "organic", "direct"}
    camp = {r.label: r for r in s.by_campaign}
    assert camp["Google Ads: Roofing - Muskoka"].leads == 2 and camp["Google Ads: Roofing - Muskoka"].spend == 300.0 and camp["Google Ads: Roofing - Muskoka"].cpl == 150.0
    assert camp["Google Ads: Siding"].leads == 1 and camp["Google Ads: Siding"].bad_share == 100.0
    assert camp["Meta Ads: Meta Leads"].leads == 0 and camp["Meta Ads: Meta Leads"].spend == 50.0  # spend with no leads is still shown
    # Speed to lead: g1 at 20 min and g2 at 180 min (the answered call is excluded); median 100, half within the hour.
    assert s.speed_median_min == 100.0 and s.speed_within_hour_pct == 50.0 and s.speed_n == 2
    # Compliance: g1, g2, g3 closed; call touched; o1 new today (needs 1 touch, has 0); m1 5 days old untouched -> 4 of 6
    assert s.compliance_n == 6 and s.compliance_pct == 66.7
    assert dict(s.lost_reasons) == {"price": 1, "spam": 1}
    assert s.overdue >= 1 and s.open_leads == 3


def test_period_helpers():
    p = reports.period("last_month", now=datetime(2026, 10, 15, 12, tzinfo=UTC))
    assert p.label == "September 2026" and p.start.astimezone(reports.ZoneInfo("America/Toronto")).day == 1 and p.days == 30
    p = reports.period("custom", start="2026-09-01", end="2026-09-07")
    assert p.days == 7 and "Sep 1, 2026 to Sep 7, 2026" == p.label
    p = reports.period("month:2026-02")
    assert p.label == "February 2026" and p.days == 28
    assert reports.period("bogus").key == "this_month"


def test_client_page_csv_and_pdf(client, world, login):
    t = world["tenant"]
    login("pat@roofco.test")
    page = client.get("/reports?period=last_30")
    assert page.status_code == 200
    assert "Cost per lead" in page.text or "cost per lead" in page.text  # spend shown: the agency enabled it
    assert "Google Ads: Roofing - Muskoka" in page.text and "Results" in page.text
    csv_r = client.get("/reports/leads.csv?period=last_30")
    assert csv_r.status_code == 200 and csv_r.headers["content-type"].startswith("text/csv")
    lines = csv_r.text.strip().splitlines()
    assert lines[0].startswith("client,ref,received,name") and len(lines) == 7
    assert "Other Client Lead" not in csv_r.text and "G One" in csv_r.text and "8500" in csv_r.text
    pdf = client.get(f"/reports/monthly.pdf?month={datetime.now(UTC):%Y-%m}")
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf" and pdf.content[:5] == b"%PDF-" and len(pdf.content) > 2000
    assert client.get("/reports/monthly.pdf?month=nope").status_code == 400
    # Spend hidden when the agency hasn't enabled it.
    with system_db() as conn:
        conn.execute("""UPDATE tenant SET settings = settings || '{"reports": {"show_spend": false}}' WHERE id = %s""", (t["id"],))
    page = client.get("/reports?period=last_30")
    assert "cost per lead" not in page.text


def test_agency_cross_client_report(client, world, make_user, admin_conn):
    u = make_user("jeff@agency.test", role="agency_admin", totp_enabled=True)
    secret = crypto.new_totp_secret()
    admin_conn.execute("UPDATE app_user SET totp_secret_enc = %s WHERE id = %s", (crypto.encrypt_secret(secret), u["id"]))
    client.post("/login", data={"email": "jeff@agency.test", "password": "correct-horse-battery"})
    page = client.get("/login/mfa")
    csrf = re.search(r'name="csrf_token" value="([0-9a-f]+)"', page.text).group(1)
    client.post("/login/mfa", data={"code": pyotp.TOTP(secret).now(), "csrf_token": csrf})
    page = client.get("/admin/reports?period=last_30")
    assert page.status_code == 200 and "North Ridge Roofing" in page.text and "Lakeside Painting" in page.text
    with system_db() as conn:
        rows, total, extras = reports.agency_summary(conn, reports.period("last_30"))
    assert total.leads == 7 and total.spend == 450.0 and [r.tenant["name"] for r in rows][0] == "North Ridge Roofing"
    assert extras["speed_median_min"] == 100.0
    csv_r = client.get("/admin/reports.csv?period=last_30")
    assert "Other Client Lead" in csv_r.text and "Lakeside Painting" in csv_r.text and len(csv_r.text.strip().splitlines()) == 8
    # Full report button switches into the client and lands on their report.
    r = client.post("/switch-tenant", data={"tenant_id": str(world["tenant"]["id"]), "next": "/reports?period=last_30", "csrf_token": csrf}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/reports?period=last_30"
    assert "North Ridge Roofing" in client.get("/reports?period=last_30").text

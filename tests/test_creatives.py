"""The ad behind each lead: described from the platform, shown on reports, the PDF and the lead."""

import io
import re

import pyotp
from PIL import Image

from app.auth import crypto
from app.db import system_db
from app.services import adplatforms, creatives, reports, worker


def _png(color=(255, 212, 0), size=(900, 600)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def _world(client, make_tenant, make_user, admin_conn):
    t = make_tenant("roofco", name="North Ridge Roofing", settings={"google_ads": {"customer_id": "1234567890"}})
    other = make_tenant("paintco", name="Lakeside Painting")
    make_user("pat@roofco.test", tenant_id=t["id"], role="member")
    make_user("sam@paintco.test", tenant_id=other["id"], role="member")
    url = f"/v1/intake/{t['slug']}/{t['intake_key']}"
    ids = {
        "m1": client.post(url, json={"name": "Meta One", "phone": "7055550001", "fbclid": "x", "utm_source": "facebook", "utm_medium": "paid_social", "campaign_id": "c9", "adset_id": "as1", "ad_id": "120001"}).json()["lead_id"],
        "m2": client.post(url, json={"name": "Meta Two", "phone": "7055550002", "fbclid": "y", "ad_id": "120001"}).json()["lead_id"],
        "g1": client.post(url, json={"name": "Google One", "phone": "7055550003", "gclid": "g", "ad_id": "777"}).json()["lead_id"],
        "g2": client.post(url, json={"name": "Google NoAd", "phone": "7055550004", "gclid": "h"}).json()["lead_id"],
        "gone": client.post(url, json={"name": "Deleted Ad", "phone": "7055550005", "fbclid": "z", "ad_id": "999"}).json()["lead_id"],
    }
    client.post(f"/v1/intake/{other['slug']}/{other['intake_key']}", json={"name": "Other", "phone": "7055550006", "fbclid": "q", "ad_id": "120001"})
    adplatforms.outbox.fake_ads[("meta_ads", "120001")] = {
        "name": "Spring roof check – video", "adset_name": "Homeowners 35+", "campaign_id": "c9", "campaign_name": "Roofing leads",
        "headline": "Free roof inspection", "body": "Book this week", "link_url": "https://www.facebook.com/123/posts/456", "image_url": "https://cdn.example/x.jpg", "thumb_bytes": _png(),
    }
    adplatforms.outbox.fake_ads[("google_ads", "777")] = {
        "name": None, "adset_name": "Roof repair", "campaign_id": "c1", "campaign_name": "Search – Muskoka", "headline": "Roof Repair Muskoka | Same-week quotes", "body": None,
        "link_url": "https://northridgeroofing.ca/", "image_url": None, "kind": "responsive search ad",
    }
    adplatforms.outbox.fake_ads[("meta_ads", "999")] = None  # the platform no longer has it
    return t, other, ids


def test_refresh_describes_ads_and_keeps_a_thumbnail(client, make_tenant, make_user, admin_conn):
    t, other, ids = _world(client, make_tenant, make_user, admin_conn)
    with system_db() as conn:
        assert {(w["platform"], w["ad_id"]) for w in creatives.wanted(conn)} == {("meta_ads", "120001"), ("google_ads", "777"), ("meta_ads", "999")}
        out = creatives.refresh(conn)
    assert out["looked_up"] == 4 and out["ok"] == 3 and out["missing"] == 1 and out["errors"] == []  # 120001 for both clients
    rows = {(r["tenant_id"], r["platform"], r["ad_id"]): r for r in admin_conn.execute("SELECT * FROM ad_creative").fetchall()}
    meta = rows[(t["id"], "meta_ads", "120001")]
    assert meta["status"] == "ok" and meta["name"] == "Spring roof check – video" and meta["campaign_name"] == "Roofing leads" and meta["thumb_type"] == "image/jpeg"
    im = Image.open(io.BytesIO(bytes(meta["thumb"])))
    assert max(im.size) == creatives.THUMB_PX and len(bytes(meta["thumb"])) < creatives.THUMB_MAX_BYTES
    assert rows[(t["id"], "google_ads", "777")]["name"] == "Roof repair" or rows[(t["id"], "google_ads", "777")]["headline"].startswith("Roof Repair")
    assert rows[(t["id"], "meta_ads", "999")]["status"] == "missing"
    assert rows[(other["id"], "meta_ads", "120001")]["status"] == "ok"
    # Nothing left to do, and a second pass asks the platforms nothing.
    adplatforms.outbox.ad_queries.clear()
    with system_db() as conn:
        assert creatives.refresh(conn)["looked_up"] == 0
    assert adplatforms.outbox.ad_queries == []
    # Errors are recorded, not raised, and retried later.
    adplatforms.outbox.fake_ads[("meta_ads", "120001")] = adplatforms.AdsError("Meta access token is not configured", retryable=False)
    admin_conn.execute("UPDATE ad_creative SET fetched_at = now() - interval '30 days' WHERE ad_id = '120001'")
    with system_db() as conn:
        out = creatives.refresh(conn)
    assert out["looked_up"] == 2 and len(out["errors"]) == 2
    r = admin_conn.execute("SELECT status, error, thumb IS NOT NULL AS has_thumb FROM ad_creative WHERE tenant_id = %s AND ad_id = '120001'", (t["id"],)).fetchone()
    assert r["status"] == "error" and "access token" in r["error"] and r["has_thumb"]  # the old thumbnail is kept


def test_tick_runs_the_refresh(client, make_tenant, make_user, admin_conn):
    _world(client, make_tenant, make_user, admin_conn)
    result = worker.run_tick()
    assert result["ads"]["looked_up"] == 4
    assert admin_conn.execute("SELECT count(*) AS n FROM job_run WHERE name = 'ad_creatives'").fetchone()["n"] == 1


def test_results_page_pdf_and_lead_show_the_ad(client, make_tenant, make_user, admin_conn, login):
    t, other, ids = _world(client, make_tenant, make_user, admin_conn)
    with system_db() as conn:
        creatives.refresh(conn)
        s = reports.tenant_summary(conn, t, reports.period("last_30"))
    assert [(r.platform, r.ad_id, r.leads) for r in s.by_ad] == [("meta_ads", "120001", 2), ("google_ads", "777", 1), ("meta_ads", "999", 1)]
    top = s.by_ad[0]
    assert top.name == "Spring roof check – video" and top.where == "Roofing leads · Homeowners 35+" and top.thumb_path == "/ads/meta_ads/120001/thumb" and top.link_url.startswith("https://www.facebook.com/")
    assert s.by_ad[2].name == "Ad 999" and s.by_ad[2].thumb_path is None
    assert s.ads_unattributed == 1

    login("pat@roofco.test")
    html = client.get("/reports?period=last_30").text
    assert "By ad" in html and "Spring roof check – video" in html and 'src="/ads/meta_ads/120001/thumb"' in html and "Roofing leads · Homeowners 35+" in html
    assert "Roof Repair Muskoka | Same-week quotes" in html  # Google search ad: the headline stands in for an image
    assert "View ad ↗" in html and "1 paid lead arrived without an ad id" in html
    r = client.get("/ads/meta_ads/120001/thumb")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and Image.open(io.BytesIO(r.content)).size[0] == creatives.THUMB_PX
    assert client.get("/ads/meta_ads/999/thumb").status_code == 404
    assert client.get("/ads/organic/120001/thumb").status_code == 404

    pdf = client.get("/reports/monthly.pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF") and b"/Image" in pdf.content or b"/XObject" in pdf.content

    lead = client.get(f"/leads/{ids['m1']}").text
    assert 'class="ad-strip"' in lead and "Came from this Meta ad · Roofing leads · Homeowners 35+" in lead and 'href="https://www.facebook.com/123/posts/456"' in lead
    assert "<dt>Ad id</dt><dd>120001 <span class=\"dim\">(ad set as1)</span></dd>" in lead
    gone = client.get(f"/leads/{ids['gone']}").text
    assert 'class="ad-strip"' not in gone and "<dt>Ad id</dt><dd>999</dd>" in gone

    # The other client sees only its own ad and cannot pull ours.
    client.get("/logout")
    login("sam@paintco.test")
    r = client.get("/ads/meta_ads/120001/thumb")
    assert r.status_code == 200  # paintco has its own copy of ad 120001
    admin_conn.execute("DELETE FROM ad_creative WHERE tenant_id = %s", (other["id"],))
    assert client.get("/ads/meta_ads/120001/thumb").status_code == 404


def test_admin_page_explains_the_url_parameters(client, make_tenant, make_user, admin_conn, login):
    t, other, ids = _world(client, make_tenant, make_user, admin_conn)
    u = make_user("boss@agency.test", role="agency_admin", totp_enabled=True)
    secret = crypto.new_totp_secret()
    admin_conn.execute("UPDATE app_user SET totp_secret_enc = %s WHERE id = %s", (crypto.encrypt_secret(secret), u["id"]))
    with system_db() as conn:
        creatives.refresh(conn)
    client.post("/login", data={"email": "boss@agency.test", "password": "correct-horse-battery"})
    page = client.get("/login/mfa")
    csrf = re.search(r'name="csrf_token" value="([0-9a-f]+)"', page.text).group(1)
    client.post("/login/mfa", data={"code": pyotp.TOTP(secret).now(), "csrf_token": csrf})
    html = client.get(f"/admin/tenants/{t['id']}").text
    assert "ad_id={{ad.id}}" in html and "ad_id={creative}" in html
    assert "4 leads with an ad id; 2 ads described, 1 not found on the platform" in html

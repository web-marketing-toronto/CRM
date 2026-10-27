"""The ad behind each lead: what it looked like, which campaign and ad set it sat in, where to see it.

Leads carry an ad id when Meta sends a lead form (always) or when the campaign URLs include it
(`ad_id={{ad.id}}` on Meta, `ad_id={creative}` on Google; docs/SETUP.md §4). Each minute tick looks for
ad ids the report would show that we haven't described yet, asks the platform, and keeps a copy of the
thumbnail (the platforms' image URLs expire after a while, so the bytes live in ad_creative.thumb).
"""

from __future__ import annotations

import io
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import psycopg

from app.services import adplatforms

log = logging.getLogger("crm.creatives")

THUMB_PX = 480           # longest side kept
THUMB_MAX_BYTES = 180_000
REFRESH_AFTER_DAYS = 14  # re-describe an ad this often (names and images get edited)
RETRY_ERROR_AFTER_HOURS = 6
LOOKBACK_DAYS = 120      # ads that produced a lead this long ago still appear on reports


def wanted(conn: psycopg.Connection, *, limit: int = 10) -> list[dict[str, Any]]:
    """Ad ids on recent leads that have no fresh description yet; the oldest attempt first."""
    return conn.execute(
        """
        SELECT s.tenant_id, s.platform, s.ad_id, max(s.created_at) AS last_lead
        FROM lead_source s
        JOIN tenant t ON t.id = s.tenant_id AND t.is_active
        LEFT JOIN ad_creative c ON c.tenant_id = s.tenant_id AND c.platform = s.platform AND c.ad_id = s.ad_id
        WHERE s.ad_id IS NOT NULL AND s.ad_id <> '' AND s.platform IN ('google_ads', 'meta_ads')
          AND s.created_at > now() - make_interval(days => %(lookback)s)
          AND (c.ad_id IS NULL
               OR (c.status = 'ok' AND c.fetched_at < now() - make_interval(days => %(refresh)s))
               OR (c.status = 'error' AND c.fetched_at < now() - make_interval(hours => %(retry)s))
               OR (c.status = 'pending'))
        GROUP BY 1, 2, 3
        ORDER BY min(coalesce(c.fetched_at, '1970-01-01')), max(s.created_at) DESC
        LIMIT %(limit)s
        """,
        {"lookback": LOOKBACK_DAYS, "refresh": REFRESH_AFTER_DAYS, "retry": RETRY_ERROR_AFTER_HOURS, "limit": limit},
    ).fetchall()


def _google_customer(conn: psycopg.Connection, tenant_id: UUID) -> str:
    t = conn.execute("SELECT settings FROM tenant WHERE id = %s", (tenant_id,)).fetchone()
    return str((((t or {}).get("settings") or {}).get("google_ads") or {}).get("customer_id") or "")


def shrink(data: bytes, content_type: str | None) -> tuple[bytes, str]:
    """Bring an image down to a thumbnail-sized JPEG (or PNG when it has transparency)."""
    from PIL import Image

    im = Image.open(io.BytesIO(data))
    im.load()
    im.thumbnail((THUMB_PX, THUMB_PX))
    out = io.BytesIO()
    has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
    if has_alpha:
        im.convert("RGBA").save(out, "PNG", optimize=True)
        return out.getvalue(), "image/png"
    im.convert("RGB").save(out, "JPEG", quality=82, optimize=True)
    return out.getvalue(), "image/jpeg"


def download_thumb(url: str) -> tuple[bytes, str] | None:
    try:
        r = httpx.get(url, timeout=30, follow_redirects=True)
    except httpx.HTTPError as exc:
        log.warning("thumbnail download failed %s: %s", url[:120], exc)
        return None
    if r.status_code >= 300 or not r.content:
        return None
    try:
        return shrink(r.content, r.headers.get("content-type"))
    except Exception as exc:  # noqa: BLE001 - a broken image is not worth failing the ad for
        log.warning("thumbnail unreadable %s: %s", url[:120], exc)
        return None


def describe(conn: psycopg.Connection, tenant_id: UUID, platform: str, ad_id: str) -> dict[str, Any]:
    """Ask the platform about one ad and store the answer. Returns the stored row's status."""
    now = datetime.now(UTC)
    try:
        if platform == "meta_ads":
            info = adplatforms.meta_ad(ad_id)
        else:
            customer = _google_customer(conn, tenant_id)
            if not customer:
                raise adplatforms.AdsError("no Google Ads customer id on this client", retryable=False)
            info = adplatforms.google_ad(customer, ad_id)
    except adplatforms.AdsError as exc:
        conn.execute(
            """
            INSERT INTO ad_creative (tenant_id, platform, ad_id, status, error, fetched_at) VALUES (%s, %s, %s, 'error', %s, %s)
            ON CONFLICT (tenant_id, platform, ad_id) DO UPDATE SET status = 'error', error = EXCLUDED.error, fetched_at = EXCLUDED.fetched_at
            """,
            (tenant_id, platform, ad_id, str(exc)[:300], now),
        )
        return {"status": "error", "error": str(exc)}
    if info is None:
        conn.execute(
            """
            INSERT INTO ad_creative (tenant_id, platform, ad_id, status, error, fetched_at) VALUES (%s, %s, %s, 'missing', NULL, %s)
            ON CONFLICT (tenant_id, platform, ad_id) DO UPDATE SET status = 'missing', error = NULL, fetched_at = EXCLUDED.fetched_at
            """,
            (tenant_id, platform, ad_id, now),
        )
        return {"status": "missing"}
    thumb: tuple[bytes, str] | None = None
    if info.get("thumb_bytes"):  # fake provider hands bytes straight over
        thumb = shrink(info["thumb_bytes"], None)
    elif info.get("image_url"):
        thumb = download_thumb(info["image_url"])
    conn.execute(
        """
        INSERT INTO ad_creative (tenant_id, platform, ad_id, name, adset_name, campaign_id, campaign_name, headline, body, link_url, image_url, thumb, thumb_type, status, error, fetched_at)
        VALUES (%(tid)s, %(platform)s, %(ad_id)s, %(name)s, %(adset_name)s, %(campaign_id)s, %(campaign_name)s, %(headline)s, %(body)s, %(link_url)s, %(image_url)s, %(thumb)s, %(thumb_type)s, 'ok', NULL, %(now)s)
        ON CONFLICT (tenant_id, platform, ad_id) DO UPDATE SET
            name = EXCLUDED.name, adset_name = EXCLUDED.adset_name, campaign_id = EXCLUDED.campaign_id, campaign_name = EXCLUDED.campaign_name,
            headline = EXCLUDED.headline, body = EXCLUDED.body, link_url = EXCLUDED.link_url, image_url = EXCLUDED.image_url,
            thumb = coalesce(EXCLUDED.thumb, ad_creative.thumb), thumb_type = coalesce(EXCLUDED.thumb_type, ad_creative.thumb_type),
            status = 'ok', error = NULL, fetched_at = EXCLUDED.fetched_at
        """,
        {
            "tid": tenant_id, "platform": platform, "ad_id": ad_id, "name": (info.get("name") or "")[:200] or None, "adset_name": (info.get("adset_name") or "")[:200] or None,
            "campaign_id": info.get("campaign_id"), "campaign_name": (info.get("campaign_name") or "")[:200] or None, "headline": (info.get("headline") or "")[:300] or None,
            "body": (info.get("body") or "")[:500] or None, "link_url": (info.get("link_url") or "")[:1000] or None, "image_url": (info.get("image_url") or "")[:1000] or None,
            "thumb": thumb[0] if thumb else None, "thumb_type": thumb[1] if thumb else None, "now": now,
        },
    )
    return {"status": "ok", "thumb": bool(thumb)}


def refresh(conn: psycopg.Connection, *, limit: int = 10) -> dict[str, Any]:
    """One pass: describe up to `limit` ads that need it. Cheap when there's nothing to do."""
    todo = wanted(conn, limit=limit)
    out: dict[str, Any] = {"looked_up": 0, "ok": 0, "missing": 0, "errors": []}
    for w in todo:
        res = describe(conn, w["tenant_id"], w["platform"], w["ad_id"])
        out["looked_up"] += 1
        if res["status"] == "ok":
            out["ok"] += 1
        elif res["status"] == "missing":
            out["missing"] += 1
        else:
            out["errors"].append(f"{w['platform']} {w['ad_id']}: {res.get('error', '')[:120]}")
    return out


def for_ads(conn: psycopg.Connection, tenant_id: UUID, keys: list[tuple[str, str]]) -> dict[tuple[str, str], dict[str, Any]]:
    """Stored descriptions (without the image bytes) for (platform, ad_id) pairs."""
    if not keys:
        return {}
    rows = conn.execute(
        """
        SELECT platform, ad_id, name, adset_name, campaign_id, campaign_name, headline, body, link_url, status, error, fetched_at, (thumb IS NOT NULL) AS has_thumb
        FROM ad_creative WHERE tenant_id = %s AND (platform::text, ad_id) IN (SELECT unnest(%s::text[]), unnest(%s::text[]))
        """,
        (tenant_id, [k[0] for k in keys], [k[1] for k in keys]),
    ).fetchall()
    return {(r["platform"], r["ad_id"]): dict(r) for r in rows}


def thumb(conn: psycopg.Connection, tenant_id: UUID, platform: str, ad_id: str) -> tuple[bytes, str] | None:
    r = conn.execute("SELECT thumb, thumb_type FROM ad_creative WHERE tenant_id = %s AND platform = %s AND ad_id = %s AND thumb IS NOT NULL", (tenant_id, platform, ad_id)).fetchone()
    return (bytes(r["thumb"]), r["thumb_type"] or "image/jpeg") if r else None


def thumb_path(platform: str, ad_id: str) -> str:
    return f"/ads/{platform}/{ad_id}/thumb"


def status_summary(conn: psycopg.Connection, tenant_id: UUID) -> dict[str, int]:
    """For the client's admin page: how many ads are described, missing, or failing."""
    rows = conn.execute("SELECT status, count(*) AS n FROM ad_creative WHERE tenant_id = %s GROUP BY status", (tenant_id,)).fetchall()
    out = {"ok": 0, "missing": 0, "error": 0, "pending": 0}
    for r in rows:
        out[r["status"]] = int(r["n"])
    out["leads_with_ad"] = int(conn.execute(
        "SELECT count(DISTINCT lead_id) AS n FROM lead_source WHERE tenant_id = %s AND ad_id IS NOT NULL AND ad_id <> '' AND platform IN ('google_ads', 'meta_ads')", (tenant_id,)
    ).fetchone()["n"])
    return out

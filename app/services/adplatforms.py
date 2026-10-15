"""Thin clients for the two ad platforms: conversion uploads and daily spend.

Google Ads goes through the REST API (no grpc dependency) with the same credentials the Campaign
Manager's google-ads.yaml holds: developer token, OAuth client id/secret, refresh token, and the
MCC as login-customer-id. Meta goes through the Graph API with the system-user token.

`ADS_PROVIDER=fake` records every call in `outbox` instead of sending (tests); `log` prints.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import httpx

from app.config import get_settings

log = logging.getLogger("crm.adplatforms")

GOOGLE_API = "https://googleads.googleapis.com/v19"
GRAPH = "https://graph.facebook.com/v21.0"


class AdsError(Exception):
    """A failed call; `retryable` says whether trying again later could help."""

    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


@dataclass
class Outbox:
    google_conversions: list[dict[str, Any]] = field(default_factory=list)
    meta_events: list[dict[str, Any]] = field(default_factory=list)
    spend_queries: list[dict[str, Any]] = field(default_factory=list)
    fake_spend: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # platform -> rows to return

    def clear(self) -> None:
        self.google_conversions.clear()
        self.meta_events.clear()
        self.spend_queries.clear()
        self.fake_spend.clear()


outbox = Outbox()


def sha256(value: str | None) -> str | None:
    if not value:
        return None
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()


def normalize_email_for_hash(email: str | None) -> str | None:
    if not email:
        return None
    local, _, domain = email.strip().lower().partition("@")
    if domain in ("gmail.com", "googlemail.com"):
        local = local.replace(".", "")
    return f"{local}@{domain}"


# --- Google Ads ----------------------------------------------------------------------------------

@dataclass
class GoogleCreds:
    developer_token: str
    client_id: str
    client_secret: str
    refresh_token: str
    login_customer_id: str | None = None


def _yaml_value(text: str, key: str) -> str | None:
    m = re.search(rf"^\s*{re.escape(key)}\s*:\s*(.+?)\s*$", text, re.M)
    if not m:
        return None
    return m.group(1).strip().strip("'\"") or None


def google_creds() -> GoogleCreds | None:
    s = get_settings()
    if s.google_ads_yaml:
        try:
            text = open(s.google_ads_yaml, encoding="utf-8").read()
        except OSError as exc:
            log.error("google-ads.yaml not readable: %s", exc)
            return None
        dev, cid, sec, ref = (_yaml_value(text, k) for k in ("developer_token", "client_id", "client_secret", "refresh_token"))
        if dev and cid and sec and ref:
            return GoogleCreds(dev, cid, sec, ref, (_yaml_value(text, "login_customer_id") or "").replace("-", "") or None)
        return None
    if s.google_ads_developer_token and s.google_ads_client_id and s.google_ads_client_secret and s.google_ads_refresh_token:
        return GoogleCreds(s.google_ads_developer_token, s.google_ads_client_id, s.google_ads_client_secret, s.google_ads_refresh_token, (s.google_ads_login_customer_id or "").replace("-", "") or None)
    return None


_token_cache: dict[str, Any] = {}


def google_access_token(creds: GoogleCreds) -> str:
    if _token_cache.get("expires", 0) > time.time() + 60 and _token_cache.get("refresh") == creds.refresh_token:
        return _token_cache["token"]
    r = httpx.post(
        "https://oauth2.googleapis.com/token",
        data={"client_id": creds.client_id, "client_secret": creds.client_secret, "refresh_token": creds.refresh_token, "grant_type": "refresh_token"},
        timeout=20,
    )
    if r.status_code >= 300:
        raise AdsError(f"Google OAuth refresh failed: {r.status_code} {r.text[:200]}", retryable=r.status_code >= 500)
    data = r.json()
    _token_cache.update({"token": data["access_token"], "expires": time.time() + int(data.get("expires_in", 3600)), "refresh": creds.refresh_token})
    return data["access_token"]


def _google_headers(creds: GoogleCreds) -> dict[str, str]:
    h = {"Authorization": f"Bearer {google_access_token(creds)}", "developer-token": creds.developer_token, "Content-Type": "application/json"}
    if creds.login_customer_id:
        h["login-customer-id"] = creds.login_customer_id
    return h


def google_upload_conversion(customer_id: str, conversion: dict[str, Any]) -> dict[str, Any]:
    """One click conversion (gclid/gbraid/wbraid and/or hashed identifiers). Returns the API result."""
    s = get_settings()
    customer_id = customer_id.replace("-", "")
    if s.ads_provider == "fake":
        outbox.google_conversions.append({"customer_id": customer_id, **conversion})
        return {"fake": True, "results": [{"conversionAction": conversion.get("conversionAction")}]}
    if s.ads_provider == "log":
        log.info("GOOGLE conversion %s: %s", customer_id, conversion)
        return {"logged": True}
    creds = google_creds()
    if creds is None:
        raise AdsError("Google Ads credentials are not configured", retryable=False)
    try:
        r = httpx.post(f"{GOOGLE_API}/customers/{customer_id}:uploadClickConversions", json={"conversions": [conversion], "partialFailure": True}, headers=_google_headers(creds), timeout=30)
    except httpx.HTTPError as exc:
        raise AdsError(f"Google Ads unreachable: {exc}") from exc
    if r.status_code >= 300:
        raise AdsError(f"Google Ads {r.status_code}: {r.text[:300]}", retryable=r.status_code in (429, 500, 502, 503, 504))
    data = r.json()
    pf = data.get("partialFailureError")
    if pf:
        msg = pf.get("message", "partial failure")
        # Clicks that are too old or unknown will never succeed; everything else might.
        permanent = any(word in msg for word in ("TOO_OLD", "INVALID", "NOT_FOUND", "EXPIRED", "UNPARSEABLE", "CONVERSION_PRECEDES_CLICK", "DUPLICATE"))
        raise AdsError(f"Google Ads rejected the conversion: {msg[:300]}", retryable=not permanent)
    return data


def google_campaign_spend(customer_id: str, since: date, until: date) -> list[dict[str, Any]]:
    """Daily spend, clicks and impressions per campaign."""
    s = get_settings()
    customer_id = customer_id.replace("-", "")
    if s.ads_provider == "fake":
        outbox.spend_queries.append({"platform": "google_ads", "customer_id": customer_id, "since": since, "until": until})
        return list(outbox.fake_spend.get("google_ads", []))
    if s.ads_provider == "log":
        return []
    creds = google_creds()
    if creds is None:
        raise AdsError("Google Ads credentials are not configured", retryable=False)
    query = f"""
        SELECT campaign.id, campaign.name, segments.date, metrics.cost_micros, metrics.clicks, metrics.impressions
        FROM campaign
        WHERE segments.date BETWEEN '{since.isoformat()}' AND '{until.isoformat()}' AND campaign.status != 'REMOVED'
    """
    try:
        r = httpx.post(f"{GOOGLE_API}/customers/{customer_id}/googleAds:searchStream", json={"query": query}, headers=_google_headers(creds), timeout=60)
    except httpx.HTTPError as exc:
        raise AdsError(f"Google Ads unreachable: {exc}") from exc
    if r.status_code >= 300:
        raise AdsError(f"Google Ads {r.status_code}: {r.text[:300]}", retryable=r.status_code >= 500)
    rows: list[dict[str, Any]] = []
    for chunk in r.json():
        for row in chunk.get("results", []):
            rows.append({
                "campaign_id": str(row["campaign"]["id"]), "campaign_name": row["campaign"].get("name"), "date": row["segments"]["date"],
                "spend": int(row["metrics"].get("costMicros", 0)) / 1_000_000, "clicks": int(row["metrics"].get("clicks", 0)), "impressions": int(row["metrics"].get("impressions", 0)),
            })
    return rows


# --- Meta ----------------------------------------------------------------------------------------

def meta_send_event(dataset_id: str, event: dict[str, Any], *, test_event_code: str | None = None) -> dict[str, Any]:
    s = get_settings()
    if s.ads_provider == "fake":
        outbox.meta_events.append({"dataset_id": dataset_id, **event})
        return {"fake": True, "events_received": 1}
    if s.ads_provider == "log":
        log.info("META event %s: %s", dataset_id, event)
        return {"logged": True}
    if not s.meta_access_token:
        raise AdsError("Meta access token is not configured", retryable=False)
    payload: dict[str, Any] = {"data": [event], "access_token": s.meta_access_token}
    if test_event_code:
        payload["test_event_code"] = test_event_code
    try:
        r = httpx.post(f"{GRAPH}/{dataset_id}/events", json=payload, timeout=30)
    except httpx.HTTPError as exc:
        raise AdsError(f"Meta unreachable: {exc}") from exc
    if r.status_code >= 300:
        try:
            err = r.json().get("error", {})
            msg = f"{err.get('code')}: {err.get('message')}"
            retryable = int(err.get("code", 0)) in (1, 2, 4, 17, 32, 613) or r.status_code >= 500
        except ValueError:
            msg, retryable = r.text[:300], r.status_code >= 500
        raise AdsError(f"Meta rejected the event: {msg}"[:400], retryable=retryable)
    return r.json()


def meta_campaign_spend(ad_account_id: str, since: date, until: date) -> list[dict[str, Any]]:
    s = get_settings()
    account = ad_account_id if ad_account_id.startswith("act_") else f"act_{ad_account_id}"
    if s.ads_provider == "fake":
        outbox.spend_queries.append({"platform": "meta_ads", "account": account, "since": since, "until": until})
        return list(outbox.fake_spend.get("meta_ads", []))
    if s.ads_provider == "log":
        return []
    if not s.meta_access_token:
        raise AdsError("Meta access token is not configured", retryable=False)
    params = {
        "access_token": s.meta_access_token, "level": "campaign", "fields": "campaign_id,campaign_name,spend,clicks,impressions",
        "time_increment": 1, "time_range": f'{{"since":"{since.isoformat()}","until":"{until.isoformat()}"}}', "limit": 500,
    }
    rows: list[dict[str, Any]] = []
    url = f"{GRAPH}/{account}/insights"
    for _ in range(20):
        try:
            r = httpx.get(url, params=params, timeout=60)
        except httpx.HTTPError as exc:
            raise AdsError(f"Meta unreachable: {exc}") from exc
        if r.status_code >= 300:
            raise AdsError(f"Meta insights {r.status_code}: {r.text[:300]}", retryable=r.status_code >= 500)
        data = r.json()
        for row in data.get("data", []):
            rows.append({
                "campaign_id": str(row.get("campaign_id")), "campaign_name": row.get("campaign_name"), "date": row.get("date_start"),
                "spend": float(row.get("spend", 0) or 0), "clicks": int(row.get("clicks", 0) or 0), "impressions": int(row.get("impressions", 0) or 0),
            })
        nxt = (data.get("paging") or {}).get("next")
        if not nxt:
            break
        url, params = nxt, {}
    return rows

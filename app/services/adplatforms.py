"""Thin clients for the two ad platforms: conversion uploads and daily spend.

Google Ads goes through the REST API (no grpc dependency) with the same credentials the Campaign
Manager's google-ads.yaml holds: developer token, OAuth client id/secret, refresh token, and the
MCC as login-customer-id. Meta goes through the Graph API with the system-user token.

`ADS_PROVIDER=fake` records every call in `outbox` instead of sending (tests); `log` prints.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import httpx

from app.config import get_settings

log = logging.getLogger("crm.adplatforms")

# Google retires each API version about a year after release and answers a retired one with an HTML 404.
# The version is a setting (GOOGLE_ADS_API_VERSION) so a bump is a deploy, not a code change.
DEFAULT_GOOGLE_ADS_API_VERSION = "v25"


def google_api() -> str:
    v = (get_settings().google_ads_api_version or DEFAULT_GOOGLE_ADS_API_VERSION).strip().lower()
    if not v.startswith("v"):
        v = "v" + v
    return f"https://googleads.googleapis.com/{v}"
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
    fake_check: dict[str, Any] | None = None  # what google_check returns in fake mode (an AdsError if an Exception)
    fake_ads: dict[tuple[str, str], Any] = field(default_factory=dict)  # (platform, ad_id) -> ad dict, or an Exception to raise
    ad_queries: list[tuple[str, str]] = field(default_factory=list)

    def clear(self) -> None:
        self.google_conversions.clear()
        self.meta_events.clear()
        self.spend_queries.clear()
        self.fake_spend.clear()
        self.fake_check = None
        self.fake_ads.clear()
        self.ad_queries.clear()


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
    """Either an OAuth refresh token (client_id/client_secret/refresh_token) or a service account
    (sa_key: the parsed JSON key, optionally impersonating a Workspace user). The yaml decides which."""

    developer_token: str
    client_id: str = ""
    client_secret: str = ""
    refresh_token: str = ""
    login_customer_id: str | None = None
    sa_key: dict[str, Any] | None = None
    impersonate: str | None = None

    @property
    def kind(self) -> str:
        return "service account" if self.sa_key else "refresh token"

    @property
    def identity(self) -> str:
        if self.sa_key:
            return self.impersonate or str(self.sa_key.get("client_email", "service account"))
        return "OAuth refresh token"

    @property
    def cache_key(self) -> str:
        return (self.sa_key or {}).get("private_key_id", "") + (self.impersonate or "") + self.refresh_token


def _yaml_value(text: str, key: str) -> str | None:
    m = re.search(rf"^\s*{re.escape(key)}\s*:\s*(.+?)\s*$", text, re.M)
    if not m:
        return None
    return m.group(1).strip().strip("'\"") or None


def google_creds_source() -> str | None:
    """Where credentials come from, for the dashboard: 'google-ads.yaml (service account)', 'env', or None."""
    s = get_settings()
    creds = google_creds()
    if creds is None:
        return None
    return f"google-ads.yaml, {creds.kind}" if s.google_ads_yaml else "env"


def _find_key_file(declared: str | None, yaml_path: str) -> str | None:
    """The yaml's json_key_file_path is the Campaign Manager's path on its VM. Here the key is mounted from
    Secret Manager, so try: GOOGLE_ADS_SA_KEY, the path as written, then the same file name next to the yaml."""
    s = get_settings()
    candidates = [s.google_ads_sa_key, declared]
    if declared:
        candidates.append(os.path.join(os.path.dirname(yaml_path), os.path.basename(declared)))
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def google_creds() -> GoogleCreds | None:
    s = get_settings()
    if s.google_ads_yaml:
        try:
            text = open(s.google_ads_yaml, encoding="utf-8").read()
        except OSError as exc:
            log.error("google-ads.yaml not readable: %s", exc)
            return None
        dev = _yaml_value(text, "developer_token")
        mcc = (_yaml_value(text, "login_customer_id") or "").replace("-", "") or None
        if not dev:
            return None
        key_path = _find_key_file(_yaml_value(text, "json_key_file_path"), s.google_ads_yaml)
        if key_path:
            try:
                key = json.load(open(key_path, encoding="utf-8"))
            except (OSError, ValueError) as exc:
                log.error("service account key not readable: %s", exc)
                return None
            if key.get("client_email") and key.get("private_key"):
                return GoogleCreds(dev, login_customer_id=mcc, sa_key=key, impersonate=_yaml_value(text, "impersonated_email"))
            return None
        cid, sec, ref = (_yaml_value(text, k) for k in ("client_id", "client_secret", "refresh_token"))
        if cid and sec and ref:
            return GoogleCreds(dev, cid, sec, ref, mcc)
        if _yaml_value(text, "json_key_file_path"):
            log.error("google-ads.yaml names a json_key_file_path but no key file is mounted (set GOOGLE_ADS_SA_KEY)")
        return None
    if s.google_ads_developer_token and s.google_ads_client_id and s.google_ads_client_secret and s.google_ads_refresh_token:
        return GoogleCreds(s.google_ads_developer_token, s.google_ads_client_id, s.google_ads_client_secret, s.google_ads_refresh_token, (s.google_ads_login_customer_id or "").replace("-", "") or None)
    return None


_token_cache: dict[str, Any] = {}
ADS_SCOPE = "https://www.googleapis.com/auth/adwords"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def service_account_assertion(key: dict[str, Any], *, scope: str = ADS_SCOPE, subject: str | None = None, now: float | None = None) -> str:
    """A signed JWT (RS256) that Google's token endpoint swaps for an access token."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    iat = int(now or time.time())
    header = {"alg": "RS256", "typ": "JWT", "kid": key.get("private_key_id")}
    claims = {"iss": key["client_email"], "scope": scope, "aud": key.get("token_uri") or "https://oauth2.googleapis.com/token", "iat": iat, "exp": iat + 3600}
    if subject:
        claims["sub"] = subject
    signing_input = _b64url(json.dumps(header, separators=(",", ":")).encode()) + "." + _b64url(json.dumps(claims, separators=(",", ":")).encode())
    private_key = serialization.load_pem_private_key(key["private_key"].encode(), password=None)
    signature = private_key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    return signing_input + "." + _b64url(signature)


def google_access_token(creds: GoogleCreds) -> str:
    if _token_cache.get("expires", 0) > time.time() + 60 and _token_cache.get("who") == creds.cache_key:
        return _token_cache["token"]
    if creds.sa_key:
        data = {"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": service_account_assertion(creds.sa_key, subject=creds.impersonate)}
        token_url = creds.sa_key.get("token_uri") or "https://oauth2.googleapis.com/token"
    else:
        data = {"client_id": creds.client_id, "client_secret": creds.client_secret, "refresh_token": creds.refresh_token, "grant_type": "refresh_token"}
        token_url = "https://oauth2.googleapis.com/token"
    try:
        r = httpx.post(token_url, data=data, timeout=20)
    except httpx.HTTPError as exc:
        raise AdsError(f"Google sign-in unreachable: {exc}") from exc
    if r.status_code >= 300:
        raise AdsError(f"Google sign-in failed ({creds.kind}): {r.status_code} {r.text[:200]}", retryable=r.status_code >= 500)
    payload = r.json()
    _token_cache.update({"token": payload["access_token"], "expires": time.time() + int(payload.get("expires_in", 3600)), "who": creds.cache_key})
    return payload["access_token"]


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
        r = httpx.post(f"{google_api()}/customers/{customer_id}:uploadClickConversions", json={"conversions": [conversion], "partialFailure": True}, headers=_google_headers(creds), timeout=30)
    except httpx.HTTPError as exc:
        raise AdsError(f"Google Ads unreachable: {exc}") from exc
    if r.status_code >= 300:
        raise AdsError(_google_error(r), retryable=r.status_code in (429, 500, 502, 503, 504))
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
        r = httpx.post(f"{google_api()}/customers/{customer_id}/googleAds:searchStream", json={"query": query}, headers=_google_headers(creds), timeout=60)
    except httpx.HTTPError as exc:
        raise AdsError(f"Google Ads unreachable: {exc}") from exc
    if r.status_code >= 300:
        raise AdsError(_google_error(r), retryable=r.status_code >= 500)
    rows: list[dict[str, Any]] = []
    for chunk in r.json():
        for row in chunk.get("results", []):
            rows.append({
                "campaign_id": str(row["campaign"]["id"]), "campaign_name": row["campaign"].get("name"), "date": row["segments"]["date"],
                "spend": int(row["metrics"].get("costMicros", 0)) / 1_000_000, "clicks": int(row["metrics"].get("clicks", 0)), "impressions": int(row["metrics"].get("impressions", 0)),
            })
    return rows


def _google_error(r: httpx.Response) -> str:
    """Google's error JSON into one line a person can act on."""
    try:
        err = r.json().get("error", {})
    except ValueError:
        if r.status_code == 404 and "<html" in r.text[:200].lower():
            v = google_api().rsplit("/", 1)[-1]
            return f"Google Ads API {v} has been retired (Google keeps each version about a year). Set GOOGLE_ADS_API_VERSION to a current version in deploy/deploy.sh and redeploy."
        return f"Google Ads {r.status_code}: {r.text[:200]}"
    codes: list[str] = []
    messages: list[str] = []
    for d in err.get("details", []):
        for e in d.get("errors", []):
            code = e.get("errorCode") or {}
            codes.extend(str(v) for v in code.values())
            if e.get("message"):
                messages.append(e["message"])
    code = ", ".join(codes) or err.get("status", str(r.status_code))
    hints = {
        "DEVELOPER_TOKEN_NOT_APPROVED": "the developer token only works on test accounts until Google approves it",
        "USER_PERMISSION_DENIED": "the MCC behind the refresh token does not manage this customer id",
        "CUSTOMER_NOT_FOUND": "no Google Ads account has this customer id",
        "NOT_ADS_USER": "the Google account behind the refresh token is not a Google Ads user",
        "OAUTH_TOKEN_REVOKED": "the refresh token was revoked; make a new google-ads.yaml",
        "DEVELOPER_TOKEN_PROHIBITED": "this developer token can't be used with this OAuth client",
        "INVALID_CUSTOMER_ID": "the customer id should be 10 digits",
        "CUSTOMER_NOT_ENABLED": "this Google Ads account is cancelled or suspended",
    }
    hint = next((h for c, h in hints.items() if c in code), None)
    detail = messages[0] if messages else err.get("message", "")
    return f"{code}: {hint or detail}"[:400]


# How a conversion action is recorded, in words a person setting it up will recognise. Only UPLOAD_CLICKS
# actions ("Import → CRMs, files, or other data sources → Track conversions from clicks") accept what the
# CRM sends; everything else is fed by a tag, a call, or an app and rejects uploads.
ACTION_TYPES = {
    "UPLOAD_CLICKS": "import from clicks (CRM)",
    "UPLOAD_CALLS": "import of calls (CallRail)",
    "WEBPAGE": "website tag",
    "WEBPAGE_CODELESS": "website tag",
    "WEBSITE_CALL": "calls from the website",
    "AD_CALL": "calls from ads",
    "CLICK_TO_CALL": "click to call",
    "LEAD_FORM_SUBMIT": "lead form asset",
    "GOOGLE_ANALYTICS_4_CUSTOM": "Google Analytics",
    "GOOGLE_ANALYTICS_4_PURCHASE": "Google Analytics",
    "STORE_VISITS": "store visits",
    "STORE_SALES": "store sales",
    "FIREBASE_ANDROID_FIRST_OPEN": "app",
    "FIREBASE_IOS_FIRST_OPEN": "app",
    "ANDROID_APP_PRE_REGISTRATION": "app",
}


def describe_action(a: dict[str, Any]) -> dict[str, Any]:
    kind = str(a.get("type", ""))
    return {
        "id": str(a.get("id", "")), "name": a.get("name", ""), "status": a.get("status", ""), "type": kind,
        "type_label": ACTION_TYPES.get(kind, kind.lower().replace("_", " ")), "uploadable": kind == "UPLOAD_CLICKS",
        "category": a.get("category", ""), "primary": bool(a.get("primaryForGoal")),
    }


def google_query(customer_id: str, gaql: str, *, headers: dict[str, str] | None = None, timeout: int = 30) -> list[dict[str, Any]]:
    """Run one GAQL query through searchStream and return the flattened result rows."""
    customer_id = re.sub(r"\D", "", customer_id or "")
    if headers is None:
        creds = google_creds()
        if creds is None:
            raise AdsError("No Google Ads credentials on this server", retryable=False)
        headers = _google_headers(creds)
    try:
        r = httpx.post(f"{google_api()}/customers/{customer_id}/googleAds:searchStream", json={"query": gaql}, headers=headers, timeout=timeout)
    except httpx.HTTPError as exc:
        raise AdsError(f"Google Ads unreachable: {exc}") from exc
    if r.status_code >= 300:
        raise AdsError(_google_error(r), retryable=r.status_code >= 500)
    return [row for chunk in r.json() for row in chunk.get("results", [])]


def google_check(customer_id: str) -> dict[str, Any]:
    """Prove the Google Ads hookup for one client without sending anything: credentials present, OAuth
    works, the MCC can see the account, and which upload-type conversion actions exist. Raises AdsError
    with a plain-language reason at whichever step fails."""
    s = get_settings()
    customer_id = re.sub(r"\D", "", customer_id or "")
    if len(customer_id) != 10:
        raise AdsError("Enter the client's 10-digit Google Ads customer id first", retryable=False)
    if s.ads_provider == "fake":
        if isinstance(outbox.fake_check, Exception):
            raise outbox.fake_check
        return outbox.fake_check or {"creds": "env", "login_customer_id": "1112223333", "account": {"id": customer_id, "name": "Fake Client", "currency": "CAD", "time_zone": "America/Toronto", "manager": False}, "actions": [], "spend_7d": 0.0}
    if s.ads_provider == "log":
        raise AdsError("ADS_PROVIDER is 'log' on this server; nothing is really sent to Google", retryable=False)
    creds = google_creds()
    if creds is None:
        raise AdsError("No Google Ads credentials on this server: the crm-google-ads-yaml secret is not deployed (deploy/deploy.sh mounts it when it exists)", retryable=False)
    headers = _google_headers(creds)  # raises AdsError if the OAuth refresh fails

    def query(gaql: str, timeout: int = 30) -> list[dict[str, Any]]:
        return google_query(customer_id, gaql, headers=headers, timeout=timeout)

    acct = query("SELECT customer.id, customer.descriptive_name, customer.currency_code, customer.time_zone, customer.manager FROM customer")
    c = (acct[0] if acct else {}).get("customer", {})
    account = {"id": str(c.get("id", customer_id)), "name": c.get("descriptiveName", ""), "currency": c.get("currencyCode", ""), "time_zone": c.get("timeZone", ""), "manager": bool(c.get("manager"))}
    rows = query(
        "SELECT conversion_action.id, conversion_action.name, conversion_action.status, conversion_action.type, conversion_action.category, conversion_action.primary_for_goal "
        "FROM conversion_action WHERE conversion_action.status != 'REMOVED' ORDER BY conversion_action.name"
    )
    actions = [describe_action(r["conversionAction"]) for r in rows]
    actions.sort(key=lambda a: (not a["uploadable"], a["name"].lower()))
    spend = query("SELECT metrics.cost_micros FROM customer WHERE segments.date DURING LAST_7_DAYS")
    spend_7d = sum(int(r.get("metrics", {}).get("costMicros", 0)) for r in spend) / 1_000_000
    return {"creds": google_creds_source(), "identity": creds.identity, "login_customer_id": creds.login_customer_id, "account": account, "actions": actions, "spend_7d": spend_7d}


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


# --- What an ad looked like ------------------------------------------------------------------------
# Returned dict: name, adset_name, campaign_id, campaign_name, headline, body, link_url, image_url.
# `None` means the platform has no such ad (deleted, or the id was never an ad id).

GOOGLE_AD_FIELDS = (
    "ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.ad.type, ad_group_ad.ad.final_urls, "
    "ad_group_ad.ad.responsive_search_ad.headlines, ad_group_ad.ad.responsive_search_ad.descriptions, "
    "ad_group_ad.ad.image_ad.image_url, ad_group_ad.ad.image_ad.name, "
    "ad_group_ad.ad.responsive_display_ad.marketing_images, ad_group_ad.ad.responsive_display_ad.headlines, "
    "ad_group_ad.ad.responsive_display_ad.long_headline, ad_group_ad.ad.responsive_display_ad.descriptions, "
    "ad_group_ad.ad.video_responsive_ad.headlines, ad_group_ad.ad.demand_gen_multi_asset_ad.headlines, ad_group_ad.ad.demand_gen_multi_asset_ad.marketing_images, "
    "ad_group.name, campaign.id, campaign.name"
)


def _texts(items: Any, key: str = "text", n: int = 3) -> list[str]:
    out = [str(i.get(key)) for i in (items or []) if isinstance(i, dict) and i.get(key)]
    return out[:n]


def google_ad(customer_id: str, ad_id: str) -> dict[str, Any] | None:
    s = get_settings()
    ad_id = re.sub(r"\D", "", ad_id or "")
    if s.ads_provider == "fake":
        outbox.ad_queries.append(("google_ads", ad_id))
        hit = outbox.fake_ads.get(("google_ads", ad_id))
        if isinstance(hit, Exception):
            raise hit
        return hit
    if s.ads_provider == "log":
        return None
    if not ad_id:
        return None
    rows = google_query(customer_id, f"SELECT {GOOGLE_AD_FIELDS} FROM ad_group_ad WHERE ad_group_ad.ad.id = {ad_id}")
    if not rows:
        return None
    r = rows[0]
    ad, group, camp = r.get("adGroupAd", {}).get("ad", {}), r.get("adGroup", {}), r.get("campaign", {})
    kind = str(ad.get("type", "")).lower().replace("_", " ")
    rsa, rda, img, dg = ad.get("responsiveSearchAd") or {}, ad.get("responsiveDisplayAd") or {}, ad.get("imageAd") or {}, ad.get("demandGenMultiAssetAd") or {}
    headlines = _texts(rsa.get("headlines")) or ([rda["longHeadline"]["text"]] if rda.get("longHeadline", {}).get("text") else []) or _texts(rda.get("headlines")) or _texts(dg.get("headlines")) or _texts((ad.get("videoResponsiveAd") or {}).get("headlines"))
    descriptions = _texts(rsa.get("descriptions"), n=1) or _texts(rda.get("descriptions"), n=1)
    image_url = img.get("imageUrl")
    if not image_url:
        assets = [a.get("asset") for a in (rda.get("marketingImages") or dg.get("marketingImages") or []) if isinstance(a, dict) and a.get("asset")]
        if assets:
            names = ", ".join(f"'{a}'" for a in assets[:1])
            arows = google_query(customer_id, f"SELECT asset.resource_name, asset.image_asset.full_size.url FROM asset WHERE asset.resource_name IN ({names})")
            for ar in arows:
                image_url = ((ar.get("asset") or {}).get("imageAsset") or {}).get("fullSize", {}).get("url")
                if image_url:
                    break
    final = (ad.get("finalUrls") or [None])[0]
    return {
        "name": ad.get("name") or img.get("name") or (headlines[0] if headlines else f"{kind or 'ad'} {ad_id}"),
        "adset_name": group.get("name"), "campaign_id": str(camp.get("id") or "") or None, "campaign_name": camp.get("name"),
        "headline": " | ".join(headlines) or None, "body": descriptions[0] if descriptions else None,
        "link_url": final, "image_url": image_url, "kind": kind or None,
    }


META_AD_FIELDS = (
    "id,name,effective_status,adset{name},campaign{id,name},preview_shareable_link,"
    "creative{id,name,title,body,thumbnail_url,image_url,effective_object_story_id,object_story_spec{link_data{link,message,name,picture},video_data{title,message,image_url}}}"
)


def meta_ad(ad_id: str) -> dict[str, Any] | None:
    s = get_settings()
    ad_id = re.sub(r"\D", "", ad_id or "")
    if s.ads_provider == "fake":
        outbox.ad_queries.append(("meta_ads", ad_id))
        hit = outbox.fake_ads.get(("meta_ads", ad_id))
        if isinstance(hit, Exception):
            raise hit
        return hit
    if s.ads_provider == "log":
        return None
    if not s.meta_access_token:
        raise AdsError("Meta access token is not configured", retryable=False)
    if not ad_id:
        return None
    try:
        r = httpx.get(f"{GRAPH}/{ad_id}", params={"access_token": s.meta_access_token, "fields": META_AD_FIELDS, "thumbnail_width": 600, "thumbnail_height": 600}, timeout=30)
    except httpx.HTTPError as exc:
        raise AdsError(f"Meta unreachable: {exc}") from exc
    if r.status_code >= 300:
        try:
            err = r.json().get("error", {})
        except ValueError:
            err = {}
        code, sub = int(err.get("code", 0) or 0), int(err.get("error_subcode", 0) or 0)
        if r.status_code == 404 or code == 100 or sub == 33:  # "Unsupported get request": not an ad id, or not ours
            return None
        raise AdsError(f"Meta ad lookup {r.status_code}: {err.get('message') or r.text[:200]}", retryable=code in (1, 2, 4, 17, 32, 613) or r.status_code >= 500)
    d = r.json()
    cr = d.get("creative") or {}
    spec = cr.get("object_story_spec") or {}
    link = spec.get("link_data") or {}
    video = spec.get("video_data") or {}
    story = cr.get("effective_object_story_id")
    link_url = d.get("preview_shareable_link") or (f"https://www.facebook.com/{story.replace('_', '/posts/')}" if story and "_" in story else None)
    return {
        "name": d.get("name") or cr.get("name") or f"Ad {ad_id}",
        "adset_name": (d.get("adset") or {}).get("name"), "campaign_id": str((d.get("campaign") or {}).get("id") or "") or None, "campaign_name": (d.get("campaign") or {}).get("name"),
        "headline": cr.get("title") or link.get("name") or video.get("title"), "body": cr.get("body") or link.get("message") or video.get("message"),
        "link_url": link_url, "image_url": cr.get("image_url") or link.get("picture") or video.get("image_url") or cr.get("thumbnail_url"), "kind": None,
    }

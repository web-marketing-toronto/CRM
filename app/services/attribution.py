"""Turn whatever a form plugin, Zapier zap or ad platform posts into one standard shape.

Two passes:
  1. flatten()   -> {normalized_key: string}  (nested dicts merged, bracket keys unwrapped,
                    WPForms/Elementor list-of-fields shapes handled)
  2. interpret() -> IntakeFields with the standard contact fields, the attribution block,
                    the inferred ad platform, and the untouched raw payload.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.models import Channel, Platform
from app.services.normalize import clean_text, join_name

# Keys whose innermost bracket segment is metadata rather than the value.
_SKIP_LEAF = {"title", "id", "type", "label", "field_type", "required"}
_VALUE_LEAF = {"value", "raw_value"}

ALIASES: dict[str, tuple[str, ...]] = {
    "contact_name": (
        "name", "full_name", "fullname", "your_name", "contact_name", "customer_name", "client_name", "nom",
    ),
    "first_name": ("first_name", "firstname", "fname", "first"),
    "last_name": ("last_name", "lastname", "lname", "last", "surname"),
    "phone": (
        "phone", "tel", "telephone", "phone_number", "your_phone", "mobile", "cell", "cellphone", "contact_phone",
        "number", "phonenumber", "customer_phone_number",
    ),
    "email": ("email", "your_email", "e_mail", "email_address", "contact_email", "customer_email"),
    "company": ("company", "company_name", "business", "business_name", "organization", "organisation"),
    "contact_address": ("address", "contact_address", "home_address", "street_address", "billing_address", "your_address", "street"),
    "job_address": (
        "job_address", "service_address", "project_address", "property_address", "job_location", "site_address",
        "where_is_the_job", "job_site", "location", "property", "address_of_project",
    ),
    "city": ("city", "town", "municipality"),
    "province": ("province", "state", "region"),
    "postal_code": ("postal_code", "postal", "postalcode", "zip", "zip_code", "postcode"),
    "job_type": (
        "job_type", "service", "service_type", "services", "project_type", "type_of_work", "interested_in",
        "service_needed", "what_do_you_need", "subject", "service_required", "type_of_service", "what_service",
    ),
    "message": (
        "message", "your_message", "comments", "comment", "notes", "details", "description", "project_details",
        "how_can_we_help", "tell_us_more", "additional_info", "additional_information",
    ),
    "form_name": ("form_name", "form_id", "form", "form_title"),
    "channel": ("channel", "lead_channel"),
    "submitted_at": ("submitted_at", "date", "timestamp", "created_at"),
}

ATTRIBUTION_ALIASES: dict[str, tuple[str, ...]] = {
    "gclid": ("gclid",),
    "gbraid": ("gbraid",),
    "wbraid": ("wbraid",),
    "fbclid": ("fbclid",),
    "fbc": ("fbc", "_fbc"),
    "fbp": ("fbp", "_fbp"),
    "utm_source": ("utm_source",),
    "utm_medium": ("utm_medium",),
    "utm_campaign": ("utm_campaign",),
    "utm_term": ("utm_term",),
    "utm_content": ("utm_content",),
    "landing_page": ("landing_page", "page_url", "page", "url", "source_url", "form_url", "page_name"),
    "referrer": ("referrer", "referer", "http_referer", "http_referrer"),
    "campaign_id": ("campaign_id", "campaignid"),
    "campaign_name": ("campaign_name", "campaign"),
    "adset_id": ("adset_id", "adsetid", "ad_group_id", "adgroupid", "adgroup_id"),
    "ad_id": ("ad_id", "adid", "creative_id"),
    "meta_lead_id": ("meta_lead_id", "leadgen_id", "lead_id", "fb_lead_id"),
    "google_lead_id": ("google_lead_id",),
    "callrail_call_id": ("callrail_call_id", "call_id"),
    "ip": ("ip", "remote_ip", "ip_address", "client_ip"),
    "user_agent": ("user_agent", "useragent"),
}

GOOGLE_SOURCES = {"google", "googleads", "adwords", "google_ads", "google-ads"}
META_SOURCES = {"facebook", "fb", "instagram", "ig", "meta", "facebook_ads", "meta_ads"}
PAID_MEDIUMS = {"cpc", "ppc", "paid", "paidsearch", "paid_social", "paid-social", "paidsocial", "social_paid", "display"}
SEARCH_ENGINES = ("google.", "bing.", "duckduckgo.", "yahoo.", "ecosia.")


def normalize_key(key: str) -> str:
    key = key.strip().lower()
    key = re.sub(r"[\s\-./]+", "_", key)
    key = re.sub(r"[^a-z0-9_\[\]]", "", key)
    return key.strip("_")


def _bracket_path(key: str) -> list[str]:
    """'fields[name][value]' -> ['fields', 'name', 'value']; plain keys -> [key]."""
    parts = re.findall(r"[^\[\]]+", key)
    return [p for p in parts if p] or [key]


_CONTAINERS = {"form", "meta", "fields", "form_fields", "data", "contact", "attribution", "lead", "payload", "submission"}


def _resolve(path: list[str]) -> str | None:
    """Collapse a key path like ['fields', 'name', 'value'] to the field it names, or None to skip."""
    if len(path) == 1:
        return path[0]
    leaf = path[-1]
    if leaf in _SKIP_LEAF:
        return None
    if leaf in _VALUE_LEAF:
        path = path[:-1]
        if len(path) == 1:
            return path[0]
    if path[0] == "form":
        return "form_name" if path[-1] in ("name", "title") else None
    return path[-1]


def flatten(raw: Mapping[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}

    def put(path: list[str], value: Any) -> None:
        if value is None:
            return
        if isinstance(value, Mapping):
            # {"value": "...", "title": "..."} shapes carry their value inside.
            if any(k in value for k in _VALUE_LEAF) and len(value) <= 6:
                put(path + ["value"], value.get("value", value.get("raw_value")))
                return
            for k, v in value.items():
                put(path + _bracket_path(normalize_key(str(k))), v)
            return
        if isinstance(value, list):
            if value and all(isinstance(i, Mapping) for i in value) and any(
                ("name" in i or "label" in i or "id" in i) and "value" in i for i in value
            ):
                for item in value:
                    label = item.get("name") or item.get("label") or item.get("id")
                    if label is not None:
                        put(path + [normalize_key(str(label))], item.get("value"))
                return
            scalars = [str(i) for i in value if i is not None and not isinstance(i, Mapping | list)]
            if scalars:
                put(path, ", ".join(scalars))
            return
        text = str(value)
        if text.strip() == "":
            return
        key = _resolve(path)
        if key and key not in out:
            out[key] = text

    for k, v in raw.items():
        put(_bracket_path(normalize_key(str(k))), v)

    # A single hidden field carrying the snippet's JSON blob of click IDs and UTMs.
    blob = out.pop("wmt_attr", None)
    if blob:
        try:
            parsed = json.loads(blob)
        except ValueError:
            parsed = None
        if isinstance(parsed, Mapping):
            for k, v in parsed.items():
                if v is not None and str(v).strip():
                    out.setdefault(normalize_key(str(k)), str(v))
    return out


@dataclass
class IntakeFields:
    contact_name: str | None = None
    company: str | None = None
    phone_raw: str | None = None
    email: str | None = None
    contact_address: str | None = None
    job_address: str | None = None
    job_type: str | None = None
    message: str | None = None
    form_name: str | None = None
    channel: Channel = Channel.WEBSITE_FORM
    platform: Platform = Platform.UNKNOWN
    campaign_name: str | None = None
    attribution: dict[str, str] = field(default_factory=dict)
    flat: dict[str, str] = field(default_factory=dict)


def _first(flat: Mapping[str, str], names: tuple[str, ...]) -> str | None:
    for n in names:
        v = flat.get(n)
        if v is not None and str(v).strip():
            return str(v)
    return None


def interpret(raw: Mapping[str, Any], field_map: Mapping[str, str] | None = None, default_channel: Channel = Channel.WEBSITE_FORM) -> IntakeFields:
    flat = flatten(raw)

    # Tenant-specific overrides: {"incoming_key": "standard_field"}.
    if field_map:
        for incoming, standard in field_map.items():
            k = normalize_key(incoming)
            if k in flat and standard:
                flat[normalize_key(standard)] = flat[k]

    f = IntakeFields(flat=flat)
    f.contact_name = clean_text(_first(flat, ALIASES["contact_name"]), max_len=200) or join_name(
        _first(flat, ALIASES["first_name"]), _first(flat, ALIASES["last_name"])
    )
    f.company = clean_text(_first(flat, ALIASES["company"]), max_len=200)
    f.phone_raw = clean_text(_first(flat, ALIASES["phone"]), max_len=64)
    f.email = clean_text(_first(flat, ALIASES["email"]), max_len=254)
    f.job_type = clean_text(_first(flat, ALIASES["job_type"]), max_len=120)
    f.message = clean_text(_first(flat, ALIASES["message"]), max_len=4000)
    f.form_name = clean_text(_first(flat, ALIASES["form_name"]), max_len=120)

    contact_address = clean_text(_first(flat, ALIASES["contact_address"]), max_len=300)
    city = clean_text(_first(flat, ALIASES["city"]), max_len=80)
    province = clean_text(_first(flat, ALIASES["province"]), max_len=40)
    postal = clean_text(_first(flat, ALIASES["postal_code"]), max_len=12)
    tail = ", ".join(p for p in (city, " ".join(p for p in (province, postal) if p) or None) if p)
    if contact_address and tail and tail.lower() not in contact_address.lower():
        contact_address = f"{contact_address}, {tail}"
    elif not contact_address and tail:
        contact_address = tail
    f.contact_address = contact_address
    f.job_address = clean_text(_first(flat, ALIASES["job_address"]), max_len=300)
    if f.job_address and tail and tail.lower() not in f.job_address.lower() and not re.search(r"\d", tail):
        pass  # don't append city/province to a job address the client typed separately

    channel_text = (_first(flat, ALIASES["channel"]) or "").strip().lower()
    try:
        f.channel = Channel(channel_text) if channel_text else default_channel
    except ValueError:
        f.channel = default_channel

    for standard, names in ATTRIBUTION_ALIASES.items():
        v = _first(flat, names)
        if v:
            f.attribution[standard] = clean_text(v, max_len=1000) or ""
    f.campaign_name = f.attribution.get("campaign_name") or f.attribution.get("utm_campaign")
    f.platform = infer_platform(f.channel, f.attribution)
    return f


def infer_platform(channel: Channel, attribution: Mapping[str, str]) -> Platform:
    if channel == Channel.META_LEAD_AD:
        return Platform.META_ADS
    if channel == Channel.GOOGLE_LEAD_FORM:
        return Platform.GOOGLE_ADS
    if any(attribution.get(k) for k in ("gclid", "gbraid", "wbraid")):
        return Platform.GOOGLE_ADS
    if attribution.get("fbclid") or attribution.get("meta_lead_id"):
        return Platform.META_ADS
    source = (attribution.get("utm_source") or "").lower()
    medium = (attribution.get("utm_medium") or "").lower()
    if source in GOOGLE_SOURCES and (medium in PAID_MEDIUMS or not medium):
        return Platform.GOOGLE_ADS
    if source in META_SOURCES and medium in PAID_MEDIUMS:
        return Platform.META_ADS
    if source in META_SOURCES:
        return Platform.META_ADS if attribution.get("fbc") or attribution.get("fbp") else Platform.REFERRAL
    if medium == "organic" or (source in GOOGLE_SOURCES and medium == "organic"):
        return Platform.ORGANIC
    referrer = (attribution.get("referrer") or "").lower()
    if referrer:
        if any(se in referrer for se in SEARCH_ENGINES):
            return Platform.ORGANIC
        return Platform.REFERRAL
    if channel in (Channel.WEBSITE_FORM, Channel.CALL):
        return Platform.DIRECT if attribution.get("landing_page") or channel == Channel.CALL else Platform.UNKNOWN
    return Platform.UNKNOWN

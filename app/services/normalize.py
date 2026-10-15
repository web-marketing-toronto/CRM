"""Phone, email and name normalization. Pure functions, no I/O."""

from __future__ import annotations

import re

import phonenumbers

DEFAULT_REGION = "CA"

_WS = re.compile(r"\s+")


def clean_text(value: object, *, max_len: int = 2000) -> str | None:
    """Collapse whitespace, strip, and return None for empty input."""
    if value is None:
        return None
    text = _WS.sub(" ", str(value)).strip()
    if not text:
        return None
    return text[:max_len]


def normalize_phone(value: object, region: str = DEFAULT_REGION) -> str | None:
    """Return an E.164 string ('+17055551234') or None if the input is not a usable number.

    Accepts the mess people type into forms: '(705) 555-1234', '705.555.1234 ext 2',
    '+1 705 555 1234', and what CallRail sends ('+17055551234').
    """
    text = clean_text(value, max_len=64)
    if not text:
        return None
    # Drop extensions and anything after a letter run ("ext", "x").
    text = re.split(r"(?i)\b(?:ext|extension|x)\b", text)[0]
    try:
        parsed = phonenumbers.parse(text, region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_possible_number(parsed):
        return None
    if not phonenumbers.is_valid_number(parsed):
        # Allow possible-but-unassigned NANP numbers (test data, new exchanges); reject junk.
        if parsed.country_code != 1:
            return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def format_phone_display(e164: str | None) -> str:
    if not e164:
        return ""
    try:
        parsed = phonenumbers.parse(e164, None)
    except phonenumbers.NumberParseException:
        return e164
    if parsed.country_code == 1:
        return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.NATIONAL)
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL)


_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(value: object) -> str | None:
    text = clean_text(value, max_len=254)
    if not text:
        return None
    text = text.strip("<>").strip()
    if not _EMAIL.match(text):
        return None
    local, _, domain = text.rpartition("@")
    return f"{local}@{domain.lower()}"


def join_name(first: object, last: object) -> str | None:
    parts = [p for p in (clean_text(first, max_len=100), clean_text(last, max_len=100)) if p]
    return " ".join(parts) or None


def postal_fsa(address: str | None) -> str | None:
    """Return the forward sortation area (first three characters) of a Canadian postal code found in text."""
    if not address:
        return None
    m = re.search(r"\b([A-Za-z]\d[A-Za-z])\s?\d[A-Za-z]\d\b", address)
    return m.group(1).upper() if m else None

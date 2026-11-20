"""Phone number normalisation.

Every customer is identified by an E.164 number (e.g. +94771234567) so that
"077 123 4567", "0771234567", "94771234567" and "+94 77 123 4567" all match the
same customer, whichever hotline or social channel they used.
"""

from __future__ import annotations

from urllib.parse import quote

import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberFormat


class InvalidPhoneNumber(ValueError):
    pass


def normalize_phone(raw: str | None, default_region: str = "LK") -> str:
    """Return the E.164 form of ``raw`` or raise :class:`InvalidPhoneNumber`."""
    cleaned = (raw or "").strip()
    if not cleaned:
        raise InvalidPhoneNumber("Contact number is required.")
    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]
    try:
        parsed = phonenumbers.parse(cleaned, default_region)
    except NumberParseException as exc:
        raise InvalidPhoneNumber(f"'{raw}' is not a valid phone number.") from exc
    if not phonenumbers.is_valid_number(parsed):
        raise InvalidPhoneNumber(f"'{raw}' is not a valid phone number.")
    return phonenumbers.format_number(parsed, PhoneNumberFormat.E164)


def try_normalize_phone(raw: str | None, default_region: str = "LK") -> str | None:
    try:
        return normalize_phone(raw, default_region)
    except InvalidPhoneNumber:
        return None


def display_phone(e164: str | None, default_region: str = "LK") -> str:
    """Local format for home-country numbers (077 123 4567), international otherwise."""
    if not e164:
        return ""
    try:
        parsed = phonenumbers.parse(e164, None)
    except NumberParseException:
        return e164
    region = phonenumbers.region_code_for_number(parsed)
    fmt = (
        PhoneNumberFormat.NATIONAL if region == default_region else PhoneNumberFormat.INTERNATIONAL
    )
    return phonenumbers.format_number(parsed, fmt)


def whatsapp_link(e164: str, text: str | None = None) -> str:
    """Click-to-chat link that opens WhatsApp with an optional pre-filled message."""
    link = f"https://wa.me/{e164.lstrip('+')}"
    if text:
        link += "?text=" + quote(text)
    return link


def tel_link(e164: str) -> str:
    return f"tel:{e164}"

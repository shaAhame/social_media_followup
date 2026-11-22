"""Jinja2 setup, template filters and small view helpers shared by routers."""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app import models
from app.config import get_settings
from app.db import utcnow
from app.security import csrf_token
from app.services.phone import display_phone, tel_link, whatsapp_link

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
env = templates.env

_STATIC_DIR = Path(__file__).parent / "static"


def _asset_versions() -> dict[str, str]:
    """Content hash per static file, so browsers fetch new CSS/JS after every deploy."""
    versions = {}
    for path in _STATIC_DIR.iterdir():
        if path.is_file():
            versions[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()[:10]
    return versions


_ASSET_VERSIONS = _asset_versions()


def static_url(name: str) -> str:
    return f"/static/{name}?v={_ASSET_VERSIONS.get(name, '0')}"


def _settings():
    return get_settings()


def localdt(value: datetime | None, fmt: str = "%d %b %Y, %I:%M %p") -> str:
    if not value:
        return ""
    return value.astimezone(_settings().tz).strftime(fmt).replace(" 0", " ").lstrip("0")


def smartdt(value: datetime | None) -> str:
    """'Today 2:30 PM', 'Tomorrow 10:00 AM', 'Mon 24 Nov, 10:00 AM'."""
    if not value:
        return ""
    tz = _settings().tz
    local = value.astimezone(tz)
    today = utcnow().astimezone(tz).date()
    clock = local.strftime("%I:%M %p").lstrip("0")
    if local.date() == today:
        return f"Today {clock}"
    if local.date() == today + timedelta(days=1):
        return f"Tomorrow {clock}"
    if local.date() == today - timedelta(days=1):
        return f"Yesterday {clock}"
    return local.strftime("%a %d %b, ").replace(" 0", " ") + clock


def reltime(value: datetime | None) -> str:
    if not value:
        return ""
    seconds = (value - utcnow()).total_seconds()
    future = seconds > 0
    seconds = abs(seconds)
    if seconds < 60:
        return "now"
    if seconds < 3600:
        amount = f"{int(seconds // 60)} min"
    elif seconds < 86400:
        amount = f"{int(seconds // 3600)} h"
    else:
        amount = f"{int(seconds // 86400)} d"
    return f"in {amount}" if future else f"{amount} ago"


def minutes(value: float | None) -> str:
    """Human duration from minutes: '12 min', '3.5 h', '2.1 days'."""
    if value is None:
        return "—"
    if value < 60:
        return f"{value:.0f} min"
    if value < 60 * 24:
        return f"{value / 60:.1f} h"
    return f"{value / 1440:.1f} days"


def lkr(value: int | None) -> str:
    return f"Rs {value:,}" if value is not None else ""


def phone(value: str | None) -> str:
    return display_phone(value, _settings().default_phone_region)


def followup_whatsapp(inquiry: models.Inquiry, user: models.User | None) -> str:
    settings = _settings()
    values = {
        "name": inquiry.customer.name or "there",
        "agent": user.first_name if user else "the team",
        "company": settings.company_name,
        "product": inquiry.product_text,
    }
    try:
        text = settings.whatsapp_followup_template.format(**values)
    except (KeyError, IndexError, ValueError):
        text = None
    return whatsapp_link(inquiry.customer.phone_e164, text)


def greeting() -> str:
    hour = utcnow().astimezone(_settings().tz).hour
    return "Good morning" if hour < 12 else "Good afternoon" if hour < 17 else "Good evening"


def is_overdue(value: datetime | None) -> bool:
    return bool(value and value < utcnow())


def query_with(request: Request, **updates) -> str:
    """Current query string with some parameters replaced (for pagination links)."""
    params = dict(request.query_params)
    for key, value in updates.items():
        if value is None:
            params.pop(key, None)
        else:
            params[key] = value
    return "?" + urlencode(params)


def flash(request: Request, message: str, category: str = "success") -> None:
    request.session.setdefault("_flashes", [])
    request.session["_flashes"] = [*request.session["_flashes"], [category, message]]


def pop_flashes(request: Request) -> list[list[str]]:
    return request.session.pop("_flashes", [])


env.filters.update(
    localdt=localdt,
    smartdt=smartdt,
    reltime=reltime,
    lkr=lkr,
    minutes=minutes,
    phone=phone,
)
env.globals.update(
    settings=_settings,
    csrf_token=csrf_token,
    pop_flashes=pop_flashes,
    followup_whatsapp=followup_whatsapp,
    tel_link=tel_link,
    whatsapp_link=whatsapp_link,
    is_overdue=is_overdue,
    greeting=greeting,
    static_url=static_url,
    query_with=query_with,
    today=lambda: utcnow().astimezone(_settings().tz).date(),
    Timeframe=models.Timeframe,
    InquiryStatus=models.InquiryStatus,
    PaymentType=models.PaymentType,
    LostReason=models.LostReason,
    ContactOutcome=models.ContactOutcome,
    ActivityKind=models.ActivityKind,
    ChannelKind=models.ChannelKind,
    Role=models.Role,
)


def render(request: Request, name: str, context: dict | None = None, status_code: int = 200):
    ctx = {"user": getattr(request.state, "user", None)}
    ctx.update(context or {})
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def local_today() -> date:
    return utcnow().astimezone(_settings().tz).date()

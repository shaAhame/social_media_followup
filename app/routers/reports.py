"""Management report page."""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db, utcnow
from app.models import User
from app.security import require_coordinator
from app.services.reports import build_report
from app.templating import local_today, render

router = APIRouter()
MAX_RANGE_DAYS = 366


def _parse(value: str | None, fallback: date) -> date:
    try:
        return date.fromisoformat(value) if value else fallback
    except ValueError:
        return fallback


@router.get("/reports")
def reports(
    request: Request,
    start: str | None = None,
    end: str | None = None,
    user: User = Depends(require_coordinator),
    db: Session = Depends(get_db),
):
    today = local_today()
    start_d = _parse(start, today.replace(day=1))
    end_d = _parse(end, today)
    if end_d < start_d:
        start_d, end_d = end_d, start_d
    if (end_d - start_d).days > MAX_RANGE_DAYS:
        start_d = end_d - timedelta(days=MAX_RANGE_DAYS)
    report = build_report(db, start_d, end_d, get_settings(), utcnow())
    presets = {
        "Today": (today, today),
        "Last 7 days": (today - timedelta(days=6), today),
        "This month": (today.replace(day=1), today),
        "Last 30 days": (today - timedelta(days=29), today),
    }
    return render(request, "reports.html", {"report": report, "presets": presets})

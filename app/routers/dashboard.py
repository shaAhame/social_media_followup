"""Home dashboard."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db, utcnow
from app.models import PushSubscription, User
from app.security import get_current_user
from app.services.dashboard import build_dashboard
from app.templating import render

router = APIRouter()


@router.get("/")
def dashboard(
    request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    settings = get_settings()
    now = utcnow()
    data = build_dashboard(db, user, settings, now)
    has_device = bool(
        db.scalar(
            select(func.count())
            .select_from(PushSubscription)
            .where(PushSubscription.user_id == user.id)
        )
    )
    return render(
        request,
        "dashboard.html",
        {"d": data, "has_device": has_device, "now_local": now.astimezone(settings.tz)},
    )

"""Home dashboard: today's numbers and the user's call list."""

from __future__ import annotations

from datetime import datetime, time

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.db import get_db, utcnow
from app.models import OPEN_STATUSES, Inquiry, InquiryStatus, User
from app.security import get_current_user
from app.services.followup import local_day_bounds
from app.services.inquiries import followup_queues
from app.templating import render

router = APIRouter()


@router.get("/")
def dashboard(
    request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    settings = get_settings()
    now = utcnow()
    start_today, _ = local_day_bounds(now, settings.tz)
    local_today = now.astimezone(settings.tz).date()
    month_start = datetime.combine(local_today.replace(day=1), time.min, settings.tz)

    owner_id = None if user.can_coordinate else user.id
    queues = followup_queues(db, owner_id, settings, now, upcoming_days=0)

    def scoped(stmt):
        return stmt if owner_id is None else stmt.where(Inquiry.assigned_to_id == owner_id)

    def count(stmt) -> int:
        return db.scalar(scoped(stmt)) or 0

    base = select(func.count()).select_from(Inquiry)
    month_total = count(base.where(Inquiry.created_at >= month_start))
    month_won = count(
        base.where(Inquiry.created_at >= month_start, Inquiry.status == InquiryStatus.WON)
    )
    stats = {
        "new_today": count(base.where(Inquiry.created_at >= start_today)),
        "due_today": len(queues.due_today),
        "overdue": len(queues.overdue),
        "needs_decision": len(queues.needs_decision),
        "open": count(base.where(Inquiry.status.in_(OPEN_STATUSES))),
        "month_total": month_total,
        "month_won": month_won,
        "month_conversion": round(100 * month_won / month_total, 1) if month_total else 0,
        "unassigned": db.scalar(
            base.where(Inquiry.status.in_(OPEN_STATUSES), Inquiry.assigned_to_id.is_(None))
        )
        or 0,
    }
    recent = []
    if user.can_coordinate:
        recent = list(
            db.scalars(
                select(Inquiry)
                .options(
                    joinedload(Inquiry.customer),
                    joinedload(Inquiry.channel),
                    joinedload(Inquiry.assigned_to),
                )
                .order_by(Inquiry.created_at.desc())
                .limit(10)
            )
        )
    call_list = (queues.overdue + queues.due_today)[:15]
    return render(
        request,
        "dashboard.html",
        {"stats": stats, "call_list": call_list, "recent": recent, "scoped": owner_id is not None},
    )

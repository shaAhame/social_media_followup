"""Call lists: overdue, due today, upcoming and leads that need a decision."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db, utcnow
from app.models import User
from app.security import get_current_user
from app.services.inquiries import followup_queues
from app.services.lookups import active_users
from app.templating import render

router = APIRouter()


@router.get("/followups")
def followups(
    request: Request,
    owner: str | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if owner is None:
        owner = "all" if user.can_coordinate else "me"
    if owner == "me":
        owner_id = user.id
    elif owner == "unassigned":
        owner_id = 0
    elif owner.isdigit():
        owner_id = int(owner)
    else:
        owner, owner_id = "all", None
    queues = followup_queues(db, owner_id, get_settings(), utcnow())
    return render(
        request,
        "followups.html",
        {"queues": queues, "owner": owner, "sales_people": active_users(db)},
    )

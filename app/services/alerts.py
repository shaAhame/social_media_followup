"""Instant notifications triggered by something a person did (not by the daily schedule)."""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.config import Settings, get_settings
from app.db import SessionLocal, get_engine
from app.models import Inquiry, User
from app.services.push import Notification, send_to_user

log = logging.getLogger(__name__)


def build_assignment_alert(
    inquiry: Inquiry, actor: User | None, settings: Settings
) -> Notification:
    from app.templating import smartdt  # local import: templating imports services

    who = inquiry.customer.name or "New customer"
    body = f"Buying: {inquiry.timeframe_label} · from {inquiry.channel.name}"
    if actor:
        body += f" · assigned by {actor.first_name}"
    if inquiry.next_followup_at:
        body += f". Follow up: {smartdt(inquiry.next_followup_at)}"
    return Notification(
        title=f"New lead for you: {who} – {inquiry.product_text}",
        body=body + ".",
        url=f"/inquiries/{inquiry.id}",
        tag=f"lead-{inquiry.id}",
    )


def notify_assignment(
    db: Session, inquiry_id: int, actor_id: int | None, settings: Settings
) -> int:
    """Tell the assigned salesperson about a lead (not on self-assignment). Returns devices."""
    inquiry = db.scalar(
        select(Inquiry)
        .options(
            joinedload(Inquiry.customer),
            joinedload(Inquiry.channel),
            joinedload(Inquiry.assigned_to).selectinload(User.push_subscriptions),
        )
        .where(Inquiry.id == inquiry_id)
    )
    if inquiry is None or not inquiry.is_open or inquiry.assigned_to is None:
        return 0
    assignee = inquiry.assigned_to
    if assignee.id == actor_id or not assignee.notify_assigned or not assignee.push_subscriptions:
        return 0
    actor = db.get(User, actor_id) if actor_id else None
    delivered = send_to_user(
        db, assignee, build_assignment_alert(inquiry, actor, settings), settings
    )
    db.commit()
    return delivered


def notify_assignment_task(inquiry_id: int, actor_id: int | None) -> None:
    """Background-task entry point: runs after the response, in its own DB session."""
    get_engine()
    try:
        with SessionLocal() as db:
            notify_assignment(db, inquiry_id, actor_id, get_settings())
    except Exception:  # a failed notification must never surface as an error to the user
        log.exception("Assignment notification for inquiry %s failed", inquiry_id)

"""Reference-data queries shared by several pages."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Channel, User


def active_channels(db: Session) -> list[Channel]:
    return list(
        db.scalars(
            select(Channel)
            .where(Channel.is_active.is_(True))
            .order_by(Channel.sort_order, Channel.name)
        )
    )


def active_users(db: Session) -> list[User]:
    return list(db.scalars(select(User).where(User.is_active.is_(True)).order_by(User.full_name)))

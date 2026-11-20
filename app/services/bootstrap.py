"""First-run setup: default lead sources and the first admin account."""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Channel, ChannelKind, Role, User
from app.security import hash_password

log = logging.getLogger(__name__)

DEFAULT_CHANNELS: list[tuple[str, ChannelKind, int]] = [
    ("WhatsApp Hotline 1", ChannelKind.WHATSAPP, 10),
    ("WhatsApp Hotline 2", ChannelKind.WHATSAPP, 20),
    ("WhatsApp Hotline 3", ChannelKind.WHATSAPP, 30),
    ("WhatsApp Hotline 4", ChannelKind.WHATSAPP, 40),
    ("WhatsApp Hotline 5", ChannelKind.WHATSAPP, 50),
    ("Facebook", ChannelKind.FACEBOOK, 60),
    ("Instagram", ChannelKind.INSTAGRAM, 70),
    ("TikTok", ChannelKind.TIKTOK, 80),
    ("Phone call", ChannelKind.PHONE_CALL, 90),
    ("Walk-in", ChannelKind.WALK_IN, 100),
    ("Website", ChannelKind.WEBSITE, 110),
]


def seed_default_channels(db: Session) -> int:
    if db.scalar(select(func.count()).select_from(Channel)):
        return 0
    for name, kind, order in DEFAULT_CHANNELS:
        db.add(Channel(name=name, kind=kind, sort_order=order))
    db.flush()
    return len(DEFAULT_CHANNELS)


def ensure_initial_admin(db: Session, settings: Settings) -> User | None:
    if db.scalar(select(func.count()).select_from(User)):
        return None
    if not (settings.initial_admin_email and settings.initial_admin_password):
        log.warning(
            "No users exist. Set INITIAL_ADMIN_EMAIL and INITIAL_ADMIN_PASSWORD, or run "
            "`python -m app.cli create-user --role admin`."
        )
        return None
    user = User(
        full_name=settings.initial_admin_name,
        email=settings.initial_admin_email.strip().lower(),
        password_hash=hash_password(settings.initial_admin_password),
        role=Role.ADMIN,
    )
    db.add(user)
    db.flush()
    log.info("Created initial admin %s", user.email)
    return user


def run_bootstrap(db: Session, settings: Settings) -> None:
    if settings.seed_default_channels:
        created = seed_default_channels(db)
        if created:
            log.info("Seeded %d default lead sources", created)
    ensure_initial_admin(db, settings)
    db.commit()

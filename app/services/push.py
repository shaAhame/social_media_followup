"""Phone notifications (Web Push).

Works with the home-screen app on Android and iPhone (iOS 16.4+, app added to the
home screen) and with desktop Chrome/Edge/Firefox. No third-party account is needed:
the app signs messages with its own VAPID key pair, generated on first use and kept
in the ``app_settings`` table.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pywebpush import WebPushException, webpush
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import utcnow
from app.models import AppSetting, PushSubscription, User

log = logging.getLogger(__name__)

PRIVATE_KEY = "vapid_private_key"
PUBLIC_KEY = "vapid_public_key"
MAX_FAILURES = 5


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def generate_vapid_keys() -> tuple[str, str]:
    """Return (private, public) as base64url: raw 32-byte scalar / uncompressed point."""
    key = ec.generate_private_key(ec.SECP256R1())
    private = key.private_numbers().private_value.to_bytes(32, "big")
    public = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return _b64url(private), _b64url(public)


def vapid_keys(db: Session) -> tuple[str, str]:
    """The app's signing keys, created and stored on first use."""
    stored = {
        row.key: row.value
        for row in db.scalars(
            select(AppSetting).where(AppSetting.key.in_([PRIVATE_KEY, PUBLIC_KEY]))
        )
    }
    if PRIVATE_KEY in stored and PUBLIC_KEY in stored:
        return stored[PRIVATE_KEY], stored[PUBLIC_KEY]
    private, public = generate_vapid_keys()
    db.merge(AppSetting(key=PRIVATE_KEY, value=private))
    db.merge(AppSetting(key=PUBLIC_KEY, value=public))
    db.commit()
    log.info("Generated notification signing keys")
    return private, public


@dataclass(frozen=True)
class Notification:
    title: str
    body: str
    url: str = "/"
    tag: str | None = None  # same tag replaces the previous notification instead of stacking

    def to_json(self) -> str:
        return json.dumps(
            {"title": self.title, "body": self.body, "url": self.url, "tag": self.tag}
        )


def save_subscription(
    db: Session, user: User, endpoint: str, p256dh: str, auth: str, device_label: str | None
) -> PushSubscription:
    """Register (or re-assign) a device. One endpoint = one device."""
    sub = db.scalar(select(PushSubscription).where(PushSubscription.endpoint == endpoint))
    if sub is None:
        sub = PushSubscription(endpoint=endpoint)
        db.add(sub)
    sub.user_id = user.id
    sub.p256dh = p256dh
    sub.auth = auth
    sub.device_label = (device_label or "")[:120] or None
    sub.failure_count = 0
    db.flush()
    return sub


def _send_one(sub: PushSubscription, payload: str, private_key: str, settings: Settings) -> bool:
    webpush(
        subscription_info={
            "endpoint": sub.endpoint,
            "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
        },
        data=payload,
        vapid_private_key=private_key,
        vapid_claims={"sub": settings.vapid_subject},
        ttl=6 * 3600,
        timeout=10,
    )
    return True


def send_to_user(db: Session, user: User, note: Notification, settings: Settings) -> int:
    """Send to every device of ``user``. Returns how many devices accepted it.

    Devices the push service reports as gone (404/410) are removed; others are removed
    after repeated failures. The caller commits.
    """
    if not settings.notifications_enabled or not user.is_active:
        return 0
    private_key, _ = vapid_keys(db)
    payload = note.to_json()
    delivered = 0
    for sub in list(user.push_subscriptions):
        try:
            _send_one(sub, payload, private_key, settings)
        except WebPushException as exc:
            status = getattr(exc.response, "status_code", None)
            if status in (404, 410):
                log.info("Removing expired notification device %s for %s", sub.id, user.email)
                db.delete(sub)
                continue
            sub.failure_count += 1
            log.warning("Push to %s failed (%s): %s", user.email, status, exc)
            if sub.failure_count >= MAX_FAILURES:
                db.delete(sub)
            continue
        except Exception as exc:  # network errors etc. must not break the daily job
            sub.failure_count += 1
            log.warning("Push to %s failed: %s", user.email, exc)
            continue
        sub.failure_count = 0
        sub.last_success_at = utcnow()
        delivered += 1
    db.flush()
    return delivered

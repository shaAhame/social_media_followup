"""Home-screen app (manifest + service worker) and phone-notification settings."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db, utcnow
from app.models import PushSubscription, User
from app.security import get_current_user, verify_csrf, verify_csrf_header
from app.services import digests
from app.services.push import Notification, save_subscription, send_to_user, vapid_keys
from app.templating import flash, render

router = APIRouter()
STATIC = Path(__file__).resolve().parent.parent / "static"


# ---------------------------------------------------------------- installable app
@router.get("/manifest.webmanifest", include_in_schema=False)
def manifest():
    settings = get_settings()
    icons = "/static/icons/"
    return JSONResponse(
        {
            "name": f"{settings.company_name} Leads",
            "short_name": "Leads",
            "description": "Inquiry follow-ups for every hotline and social channel",
            "start_url": "/?source=homescreen",
            "scope": "/",
            "display": "standalone",
            "background_color": "#f6f7f9",
            "theme_color": "#1f4fd8",
            "icons": [
                {"src": icons + "icon-192.png", "sizes": "192x192", "type": "image/png"},
                {"src": icons + "icon-512.png", "sizes": "512x512", "type": "image/png"},
                {
                    "src": icons + "icon-maskable-512.png",
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "maskable",
                },
            ],
            "shortcuts": [
                {"name": "New inquiry", "url": "/inquiries/new"},
                {"name": "My follow-ups", "url": "/followups?owner=me"},
            ],
        },
        media_type="application/manifest+json",
    )


@router.get("/sw.js", include_in_schema=False)
def service_worker():
    return FileResponse(
        STATIC / "sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )


# ---------------------------------------------------------------- notification settings
@router.get("/account/notifications")
def notifications_page(
    request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    settings = get_settings()
    _, public_key = vapid_keys(db)
    devices = list(
        db.scalars(
            select(PushSubscription)
            .where(PushSubscription.user_id == user.id)
            .order_by(PushSubscription.created_at.desc())
        )
    )
    return render(
        request,
        "account/notifications.html",
        {
            "public_key": public_key,
            "devices": devices,
            "morning_time": settings.morning_digest_time,
            "evening_time": settings.evening_report_time,
            "enabled": settings.notifications_enabled,
        },
    )


class SubscriptionKeys(BaseModel):
    p256dh: str = Field(..., max_length=255)
    auth: str = Field(..., max_length=255)


class SubscriptionIn(BaseModel):
    endpoint: str = Field(..., max_length=2000)
    keys: SubscriptionKeys
    device_label: str | None = Field(None, max_length=200)


@router.post("/account/notifications/subscribe", dependencies=[Depends(verify_csrf_header)])
def subscribe(
    body: SubscriptionIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    if not body.endpoint.startswith("https://"):
        raise HTTPException(422, "Invalid push endpoint.")
    save_subscription(db, user, body.endpoint, body.keys.p256dh, body.keys.auth, body.device_label)
    db.commit()
    return {"ok": True}


@router.post("/account/notifications/settings", dependencies=[Depends(verify_csrf)])
def update_settings(
    request: Request,
    notify_morning: str | None = Form(None),
    notify_evening: str | None = Form(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user.notify_morning = bool(notify_morning)
    user.notify_evening = bool(notify_evening)
    db.commit()
    flash(request, "Notification settings saved.")
    return RedirectResponse("/account/notifications", 303)


@router.post("/account/notifications/{device_id:int}/remove", dependencies=[Depends(verify_csrf)])
def remove_device(
    request: Request,
    device_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    device = db.get(PushSubscription, device_id)
    if device is None or device.user_id != user.id:
        raise HTTPException(404, "Device not found.")
    db.delete(device)
    db.commit()
    flash(request, "Device removed. It won't get notifications any more.")
    return RedirectResponse("/account/notifications", 303)


@router.post("/account/notifications/send", dependencies=[Depends(verify_csrf)])
def send_now(
    request: Request,
    kind: str = Form("test"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Send a test, or today's call list / evening report, to the current user's devices."""
    settings = get_settings()
    now = utcnow()
    if kind == "morning":
        note = digests.build_morning_digest(db, user, settings, now) or Notification(
            title=f"Good morning {user.first_name}: nothing due today",
            body="No overdue or due follow-ups right now.",
            url="/followups?owner=me",
            tag=digests.MORNING,
        )
    elif kind == "evening" and user.can_coordinate:
        note = digests.build_evening_report(db, settings, now)
    else:
        note = Notification(
            title="Notifications are working ✅",
            body=f"You'll get your call list at {settings.morning_digest_time} every morning.",
            url="/followups?owner=me",
            tag="test",
        )
    delivered = send_to_user(db, user, note, settings)
    db.commit()
    if delivered:
        flash(request, f"Sent to {delivered} device(s). Check your phone.")
    else:
        flash(request, "No device received it. Turn on notifications on this phone first.", "error")
    return RedirectResponse("/account/notifications", 303)

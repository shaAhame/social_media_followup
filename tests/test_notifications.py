import base64
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.db import utcnow
from app.models import JobRun, PushSubscription, Role, Timeframe
from app.services import digests, push
from app.services import inquiries as svc
from tests.conftest import csrf_from, login, post_form

ENDPOINT = "https://fcm.googleapis.com/fcm/send/abc123"
KEYS = {
    "p256dh": (
        "BNcRdreALRFXTkOOUHK1EtK2wtaz5Ry4YfYCA_0QTpQtUbVlUls0VJXg7A8u-Ts1XbjhazAkj7I99e8QcYP7DkM"
    ),
    "auth": "tBHItJI5svbpez7KI4CCXg",
}


@pytest.fixture
def sent(monkeypatch):
    """Capture pushes instead of contacting real push services."""
    calls = []

    def fake_send(sub, payload, private_key, settings):
        calls.append((sub.endpoint, json.loads(payload)))
        return True

    monkeypatch.setattr(push, "_send_one", fake_send)
    return calls


def _device(db, user, endpoint=ENDPOINT):
    sub = push.save_subscription(db, user, endpoint, KEYS["p256dh"], KEYS["auth"], "Android phone")
    db.commit()
    return sub


def _lead(db, settings, channel, user, phone, due_at, timeframe=Timeframe.THIS_WEEK, name="Nimal"):
    clean = svc.clean_inquiry_input(
        db,
        svc.InquiryInput(
            phone=phone,
            channel_id=channel.id,
            product_text="iPhone 17",
            timeframe=timeframe,
            customer_name=name,
            assigned_to_id=user.id if user else None,
        ),
        settings,
    )
    inquiry = svc.create_inquiry(db, clean, None, settings, utcnow())
    inquiry.next_followup_at = due_at
    db.commit()
    return inquiry


# ---------------------------------------------------------------- home-screen app
def test_manifest_service_worker_and_icons(client):
    manifest = client.get("/manifest.webmanifest")
    assert manifest.headers["content-type"].startswith("application/manifest+json")
    data = manifest.json()
    assert data["display"] == "standalone" and data["start_url"].startswith("/")
    for icon in data["icons"]:
        assert client.get(icon["src"]).status_code == 200
    sw = client.get("/sw.js")
    assert sw.status_code == 200 and "showNotification" in sw.text
    assert sw.headers["cache-control"] == "no-cache"
    page = client.get("/login").text
    assert 'rel="manifest"' in page and "apple-touch-icon" in page


# ---------------------------------------------------------------- keys & devices
def test_vapid_keys_are_generated_once(db):
    private, public = push.vapid_keys(db)
    assert push.vapid_keys(db) == (private, public)
    raw_public = base64.urlsafe_b64decode(public + "==")
    assert len(raw_public) == 65 and raw_public[0] == 4  # uncompressed P-256 point
    assert len(base64.urlsafe_b64decode(private + "==")) == 32


def test_subscribe_requires_csrf_header_and_saves_device(client, make_user, db):
    user = make_user(Role.SALES)
    login(client, user)
    body = {"endpoint": ENDPOINT, "keys": KEYS, "device_label": "iPhone (home-screen app)"}
    assert client.post("/account/notifications/subscribe", json=body).status_code == 403
    token = csrf_from(client, "/account/notifications")
    response = client.post(
        "/account/notifications/subscribe", json=body, headers={"X-CSRF-Token": token}
    )
    assert response.status_code == 200
    device = db.scalar(select(PushSubscription))
    assert device.user_id == user.id and device.device_label == "iPhone (home-screen app)"
    # the same phone re-registering doesn't create a duplicate
    client.post("/account/notifications/subscribe", json=body, headers={"X-CSRF-Token": token})
    assert db.scalar(select(func.count()).select_from(PushSubscription)) == 1
    page = client.get("/account/notifications").text
    assert "iPhone (home-screen app)" in page and "data-public-key=" in page


def test_send_handles_expired_and_failing_devices(db, settings, make_user, monkeypatch):
    from pywebpush import WebPushException

    user = make_user(Role.SALES)
    good = _device(db, user, ENDPOINT + "good")
    gone = _device(db, user, ENDPOINT + "gone")
    flaky = _device(db, user, ENDPOINT + "flaky")

    class Resp:
        def __init__(self, code):
            self.status_code = code

    def fake_send(sub, payload, private_key, settings):
        if sub.endpoint.endswith("gone"):
            raise WebPushException("gone", response=Resp(410))
        if sub.endpoint.endswith("flaky"):
            raise WebPushException("busy", response=Resp(503))
        return True

    monkeypatch.setattr(push, "_send_one", fake_send)
    delivered = push.send_to_user(db, user, push.Notification("t", "b"), settings)
    db.commit()
    assert delivered == 1
    remaining = {s.endpoint for s in db.scalars(select(PushSubscription))}
    assert remaining == {good.endpoint, flaky.endpoint}
    db.refresh(flaky)
    assert flaky.failure_count == 1
    db.refresh(good)
    assert good.last_success_at is not None
    _ = gone


# ---------------------------------------------------------------- message content
def test_morning_digest_content(db, settings, channels, make_user):
    rep = make_user(Role.SALES, name="Kasun Perera")
    now = utcnow()
    assert digests.build_morning_digest(db, rep, settings, now) is None  # nothing to do
    _lead(db, settings, channels[0], rep, "0771111111", now - timedelta(hours=5), name="Nimal")
    _lead(db, settings, channels[0], rep, "0772222222", now + timedelta(minutes=30))
    note = digests.build_morning_digest(db, rep, settings, now)
    assert note.title == "Good morning Kasun: 2 follow-ups today"
    assert "1 overdue" in note.body and "First: Nimal – iPhone 17" in note.body
    assert note.url == "/followups?owner=me"


def test_coordinator_digest_mentions_unassigned(db, settings, channels, make_user):
    coord = make_user(Role.COORDINATOR, name="Dilani Coordinator")
    _lead(db, settings, channels[0], None, "0773333333", utcnow() + timedelta(days=3))
    note = digests.build_morning_digest(db, coord, settings, utcnow())
    assert "1 unassigned lead" in note.body and note.url == "/inquiries?assigned=none"


def test_evening_report_content(db, settings, channels, make_user):
    rep = make_user(Role.SALES, name="Kasun Perera")
    now = utcnow()
    _lead(db, settings, channels[0], rep, "0771111111", now - timedelta(hours=2))
    sold = _lead(db, settings, channels[0], rep, "0772222222", now + timedelta(days=1))
    svc.log_contact(
        db, sold, rep, svc.ActivityKind.CALL, svc.ContactOutcome.WON, None, settings, now
    )
    db.commit()
    note = digests.build_evening_report(db, settings, now)
    assert note.title == "Today: 2 inquiries · 1 sold"
    assert "Overdue now: 1 (Kasun 1)" in note.body
    assert note.url.startswith("/reports?start=")


# ---------------------------------------------------------------- scheduler
def _at(hh, mm, settings):
    local = datetime(2026, 11, 23, hh, mm, tzinfo=settings.tz)
    return local.astimezone(UTC)


def test_due_jobs_window(settings):
    assert digests.due_jobs(_at(9, 44, settings), settings) == []
    assert digests.due_jobs(_at(9, 45, settings), settings) == [digests.MORNING]
    assert digests.due_jobs(_at(11, 40, settings), settings) == [digests.MORNING]  # catch-up
    assert digests.due_jobs(_at(11, 46, settings), settings) == []
    assert digests.due_jobs(_at(19, 30, settings), settings) == [digests.EVENING]


def test_jobs_run_once_per_day(db, settings, channels, make_user, sent):
    rep = make_user(Role.SALES, name="Kasun Perera")
    manager = make_user(Role.MANAGER, name="Manager Person")
    _device(db, rep, ENDPOINT + "rep")
    _device(db, manager, ENDPOINT + "mgr")
    morning = _at(9, 46, settings)
    _lead(db, settings, channels[0], rep, "0771111111", morning - timedelta(hours=1))

    assert digests.run_due_jobs(morning, settings) == {digests.MORNING: 1}
    assert digests.run_due_jobs(morning + timedelta(minutes=1), settings) == {}  # already ran
    assert [endpoint for endpoint, _ in sent] == [ENDPOINT + "rep"]

    evening = _at(19, 31, settings)
    assert digests.run_due_jobs(evening, settings) == {digests.EVENING: 1}
    assert sent[-1][0] == ENDPOINT + "mgr"  # sales staff don't get the manager report
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(JobRun)) == 2


def test_preferences_respected(db, settings, channels, make_user, sent):
    rep = make_user(Role.SALES, name="Kasun Perera")
    rep.notify_morning = False
    db.commit()
    _device(db, rep)
    morning = _at(9, 50, settings)
    _lead(db, settings, channels[0], rep, "0771111111", morning - timedelta(hours=1))
    assert digests.run_due_jobs(morning, settings) == {digests.MORNING: 0}
    assert sent == []


# ---------------------------------------------------------------- pages
def test_notifications_page_buttons_and_dashboard_nudge(client, make_user, db, sent):
    user = make_user(Role.MANAGER)
    login(client, user)
    assert "Turn on notifications" in client.get("/").text  # nudge until a device exists
    response = post_form(
        client, "/account/notifications/send", {"kind": "test"}, token_page="/account/notifications"
    )
    assert "No device received it" in client.get("/account/notifications").text
    _device(db, user)
    assert "Turn on notifications</a>" not in client.get("/").text
    for kind in ("test", "morning", "evening"):
        response = post_form(
            client,
            "/account/notifications/send",
            {"kind": kind},
            token_page="/account/notifications",
        )
        assert response.status_code == 303
    assert len(sent) == 3
    assert "Sent to 1 device" in client.get("/account/notifications").text
    post_form(
        client,
        "/account/notifications/settings",
        {"notify_morning": "1"},
        token_page="/account/notifications",
    )
    db.expire_all()
    db.refresh(user)
    assert user.notify_morning and not user.notify_evening


# ---------------------------------------------------------------- instant assignment alert
def _form(channel_id, **extra):
    return {
        "phone": "0771234567",
        "customer_name": "Nimal",
        "channel_id": channel_id,
        "product_text": "iPhone 17 Pro",
        "timeframe": "today",
        **extra,
    }


def test_salesperson_alerted_when_coordinator_assigns_new_lead(
    client, make_user, channels, db, sent
):
    coord = make_user(Role.COORDINATOR, name="Dilani Coordinator")
    rep = make_user(Role.SALES, name="Kasun Perera")
    _device(db, rep)
    login(client, coord)
    response = post_form(
        client,
        "/inquiries/new",
        _form(channels[0].id, assigned_to_id=str(rep.id)),
        token_page="/inquiries/new",
    )
    assert response.status_code == 303
    assert len(sent) == 1
    endpoint, payload = sent[0]
    assert endpoint == ENDPOINT
    assert payload["title"] == "New lead for you: Nimal – iPhone 17 Pro"
    assert "Buying: Today" in payload["body"] and channels[0].name in payload["body"]
    assert "assigned by Dilani" in payload["body"]
    assert payload["url"].startswith("/inquiries/")


def test_alert_on_reassignment_only(client, make_user, channels, db, sent):
    coord = make_user(Role.COORDINATOR, name="Dilani Coordinator")
    rep_a = make_user(Role.SALES, name="Rep A")
    rep_b = make_user(Role.SALES, name="Rep B")
    _device(db, rep_a, ENDPOINT + "a")
    _device(db, rep_b, ENDPOINT + "b")
    login(client, coord)
    post_form(client, "/inquiries/new", _form(channels[0].id), token_page="/inquiries/new")
    assert sent == []  # unassigned: nobody to tell
    inquiry_id = db.scalar(select(svc.Inquiry.id))
    page = f"/inquiries/{inquiry_id}/edit"
    edit = {**_form(channels[0].id), "status": "new"}
    post_form(client, page, {**edit, "assigned_to_id": str(rep_a.id)}, token_page=page)
    post_form(
        client, page, {**edit, "assigned_to_id": str(rep_a.id), "notes": "x"}, token_page=page
    )
    post_form(client, page, {**edit, "assigned_to_id": str(rep_b.id)}, token_page=page)
    assert [endpoint for endpoint, _ in sent] == [ENDPOINT + "a", ENDPOINT + "b"]


def test_no_alert_for_self_assignment_or_when_turned_off(client, make_user, channels, db, sent):
    coord = make_user(Role.COORDINATOR, name="Dilani Coordinator")
    rep = make_user(Role.SALES, name="Kasun Perera")
    _device(db, coord, ENDPOINT + "coord")
    _device(db, rep, ENDPOINT + "rep")
    rep.notify_assigned = False
    db.commit()
    login(client, coord)
    post_form(
        client,
        "/inquiries/new",
        _form(channels[0].id, assigned_to_id=str(coord.id)),
        token_page="/inquiries/new",
    )
    post_form(
        client,
        "/inquiries/new",
        _form(channels[0].id, assigned_to_id=str(rep.id), phone="0712345678"),
        token_page="/inquiries/new",
    )
    assert sent == []


def test_assignment_alert_setting_saved(client, make_user, db):
    rep = make_user(Role.SALES)
    login(client, rep)
    assert 'name="notify_assigned"' in client.get("/account/notifications").text
    post_form(
        client,
        "/account/notifications/settings",
        {"notify_morning": "1"},
        token_page="/account/notifications",
    )
    db.expire_all()
    db.refresh(rep)
    assert rep.notify_morning and not rep.notify_assigned

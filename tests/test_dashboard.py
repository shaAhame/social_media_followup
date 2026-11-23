from datetime import timedelta

from app.db import utcnow
from app.models import ActivityKind, ContactOutcome, Role, Timeframe
from app.services import inquiries as svc
from app.services.dashboard import build_dashboard
from tests.conftest import login


def _lead(db, settings, channel, owner, phone, timeframe=Timeframe.THIS_WEEK, due=None, **kw):
    clean = svc.clean_inquiry_input(
        db,
        svc.InquiryInput(
            phone=phone,
            channel_id=channel.id,
            product_text="iPhone 17",
            timeframe=timeframe,
            customer_name="Nimal",
            assigned_to_id=owner.id if owner else None,
            **kw,
        ),
        settings,
    )
    inquiry = svc.create_inquiry(db, clean, None, settings, utcnow())
    if due is not None:
        inquiry.next_followup_at = due
    db.commit()
    return inquiry


def test_dashboard_numbers_for_sales_are_scoped(db, settings, channels, make_user):
    rep = make_user(Role.SALES, name="Rep One")
    other = make_user(Role.SALES, name="Rep Two")
    now = utcnow()
    mine = _lead(db, settings, channels[0], rep, "0771111111", due=now - timedelta(hours=1))
    _lead(db, settings, channels[0], rep, "0772222222", Timeframe.TODAY)
    _lead(db, settings, channels[0], other, "0773333333", due=now - timedelta(hours=2))
    svc.log_contact(db, mine, rep, ActivityKind.CALL, ContactOutcome.REACHED, None, settings, now)
    db.commit()

    d = build_dashboard(db, rep, settings, now)
    assert d.scoped
    assert d.open_total == 2 and d.new_today == 2
    assert d.done_today == 1
    assert d.today_total == d.done_today + d.to_contact
    assert [i.customer.phone_e164 for i in d.hot_leads] == ["+94772222222"]
    assert d.team == [] and d.month_report is None
    assert sum(bar.inquiries for bar in d.trend) == 2 and len(d.trend) == 14


def test_dashboard_for_coordinator_has_team_and_month(db, settings, channels, make_user):
    coord = make_user(Role.COORDINATOR, name="Dilani Coordinator")
    rep = make_user(Role.SALES, name="Rep One")
    now = utcnow()
    _lead(db, settings, channels[0], rep, "0771111111", due=now - timedelta(hours=1))
    _lead(db, settings, channels[1], None, "0772222222")
    planned = (now + timedelta(days=3)).astimezone(settings.tz).date()
    _lead(db, settings, channels[1], rep, "0773333333", Timeframe.ON_DATE, planned_date=planned)

    d = build_dashboard(db, coord, settings, now)
    assert not d.scoped
    assert d.unassigned == 1 and d.open_total == 3
    team = {row.user.full_name: row for row in d.team}
    assert team["Rep One"].open == 2 and team["Rep One"].overdue == 1
    assert d.month_report.summary.total == 3
    assert len(d.latest) == 3
    assert any(i.timeframe == Timeframe.ON_DATE for i in d.hot_leads)
    assert dict((s.value, n) for s, n in d.pipeline)["new"] == 3


def test_dashboard_pages_render(client, make_user, channels, db, settings):
    coord = make_user(Role.COORDINATOR, name="Dilani Coordinator")
    rep = make_user(Role.SALES, name="Rep One")
    _lead(db, settings, channels[0], rep, "0771111111", due=utcnow() - timedelta(hours=1))
    login(client, coord)
    page = client.get("/").text
    for text in (
        "Good ",
        "Call now",
        "Ready to buy soon",
        "Open pipeline",
        "Last 14 days",
        "Team today",
        "Where leads came from",
        "Why sales were lost",
        "Latest inquiries",
    ):
        assert text in page, text
    assert "Rep One" in page
    client.cookies.clear()
    login(client, rep)
    page = client.get("/").text
    assert "Call now" in page and "Team today" not in page and "your leads" in page


def test_placeholder_names_are_not_stored(db, settings, channels):
    for raw in ("NA", "n/a", "No name given", "  -  "):
        clean = svc.clean_inquiry_input(
            db,
            svc.InquiryInput(
                phone="0771234567",
                channel_id=channels[0].id,
                product_text="x",
                timeframe="this_week",
                customer_name=raw,
            ),
            settings,
        )
        assert clean.customer_name is None, raw

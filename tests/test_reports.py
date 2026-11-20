from datetime import timedelta

from app.db import utcnow
from app.models import ActivityKind, ContactOutcome, LostReason, Role, Timeframe
from app.services import inquiries as svc
from app.services.reports import build_report, inquiries_to_csv


def _make(db, settings, channel, phone, product, created):
    clean = svc.clean_inquiry_input(
        db,
        svc.InquiryInput(
            phone=phone, channel_id=channel.id, product_text=product, timeframe=Timeframe.THIS_WEEK
        ),
        settings,
    )
    return svc.create_inquiry(db, clean, None, settings, created)


def test_report_numbers(db, settings, channels, make_user):
    rep = make_user(Role.SALES)
    now = utcnow()
    a = _make(db, settings, channels[0], "0771111111", "iPhone 16", now - timedelta(hours=5))
    b = _make(db, settings, channels[0], "0772222222", "iphone 16", now - timedelta(hours=4))
    c = _make(db, settings, channels[5], "0773333333", "Galaxy S25", now - timedelta(hours=3))
    for inquiry in (a, b, c):
        inquiry.assigned_to = rep
    svc.log_contact(
        db,
        a,
        rep,
        ActivityKind.CALL,
        ContactOutcome.WON,
        None,
        settings,
        a.created_at + timedelta(minutes=10),
    )
    svc.log_contact(
        db,
        b,
        rep,
        ActivityKind.CALL,
        ContactOutcome.LOST,
        None,
        settings,
        b.created_at + timedelta(minutes=30),
        lost_reason=LostReason.PRICE,
    )
    db.commit()

    today = now.astimezone(settings.tz).date()
    report = build_report(db, today - timedelta(days=1), today, settings, now)

    assert (report.summary.total, report.summary.won, report.summary.lost, report.summary.open) == (
        3,
        1,
        1,
        1,
    )
    assert report.summary.conversion == 33.3
    hotline = next(r for r in report.by_channel if r.label == channels[0].name)
    assert (hotline.total, hotline.won, hotline.lost) == (2, 1, 1)
    assert report.top_products[0].total == 2  # "iPhone 16" and "iphone 16" grouped
    assert report.lost_reasons == [("Price too high", 1)]
    assert report.median_first_response_min == 20.0
    assert report.pct_responded_within_15_min == 50.0
    assert report.by_user[0].label == rep.full_name
    assert sum(count for _, count in report.daily) == 3


def test_csv_contains_rows(db, settings, channels):
    inquiry = _make(db, settings, channels[0], "0771234567", "+Pixel", utcnow())
    db.commit()
    text = inquiries_to_csv([inquiry], settings)
    lines = text.strip().splitlines()
    assert len(lines) == 2
    assert "'+Pixel" in lines[1]

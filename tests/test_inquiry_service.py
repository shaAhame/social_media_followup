from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.db import utcnow
from app.models import (
    ActivityKind,
    ContactOutcome,
    Customer,
    InquiryStatus,
    LostReason,
    Role,
    Timeframe,
)
from app.services import inquiries as svc


def _create(db, settings, channel, phone="0771234567", product="iPhone 16 Pro", **kw):
    clean = svc.clean_inquiry_input(
        db,
        svc.InquiryInput(
            phone=phone,
            channel_id=channel.id,
            product_text=product,
            timeframe=kw.pop("timeframe", Timeframe.THIS_WEEK),
            **kw,
        ),
        settings,
    )
    inquiry = svc.create_inquiry(db, clean, None, settings, utcnow())
    db.commit()
    return inquiry


def test_same_customer_across_channels_is_one_customer(db, settings, channels):
    _create(db, settings, channels[0], phone="0771234567")
    _create(db, settings, channels[6], phone="+94 77 123 4567", product="AirPods Pro 2")
    assert db.scalar(select(func.count()).select_from(Customer)) == 1


def test_validation_errors_are_collected(db, settings):
    with pytest.raises(svc.DomainError) as exc:
        svc.clean_inquiry_input(
            db,
            svc.InquiryInput(phone="123", channel_id=None, product_text="", timeframe="soon"),
            settings,
        )
    assert set(exc.value.errors) == {"phone", "channel_id", "product_text", "timeframe"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("250000", 250000),
        ("250,000", 250000),
        ("Rs. 389,000", 389000),
        ("250k", 250000),
        ("1.2m", 1200000),
        ("", None),
    ],
)
def test_parse_lkr(raw, expected):
    assert svc.parse_lkr(raw, {}) == expected


def test_parse_lkr_rejects_garbage():
    errors = {}
    assert svc.parse_lkr("about two lakhs", errors) is None
    assert "budget_lkr" in errors


def test_new_inquiry_gets_scheduled_and_timeline(db, settings, channels):
    inquiry = _create(db, settings, channels[0], timeframe=Timeframe.TODAY)
    assert inquiry.status == InquiryStatus.NEW
    assert inquiry.next_followup_at is not None
    assert [a.kind for a in inquiry.activities] == [ActivityKind.CREATED]


def test_followup_moves_pipeline_and_schedules_next(db, settings, channels, make_user):
    rep = make_user(Role.SALES)
    inquiry = _create(db, settings, channels[0])
    now = utcnow()
    svc.log_contact(
        db, inquiry, rep, ActivityKind.CALL, ContactOutcome.REACHED, "Talked", settings, now
    )
    assert inquiry.status == InquiryStatus.CONTACTED
    assert inquiry.followup_count == 1
    assert inquiry.first_response_at == now
    first_next = inquiry.next_followup_at

    svc.log_contact(
        db, inquiry, rep, ActivityKind.WHATSAPP, ContactOutcome.PRICE_SENT, None, settings, now
    )
    assert inquiry.status == InquiryStatus.PRICE_SENT
    assert inquiry.next_followup_at > first_next  # cadence grows


def test_no_answer_keeps_status_but_counts_attempt(db, settings, channels, make_user):
    rep = make_user()
    inquiry = _create(db, settings, channels[0])
    svc.log_contact(
        db, inquiry, rep, ActivityKind.CALL, ContactOutcome.NO_ANSWER, None, settings, utcnow()
    )
    assert inquiry.status == InquiryStatus.NEW
    assert inquiry.followup_count == 1


def test_manual_next_date_overrides_schedule(db, settings, channels, make_user):
    rep = make_user()
    inquiry = _create(db, settings, channels[0])
    target = utcnow() + timedelta(days=10)
    svc.log_contact(
        db,
        inquiry,
        rep,
        ActivityKind.CALL,
        ContactOutcome.CALL_LATER,
        None,
        settings,
        utcnow(),
        next_at=target,
    )
    assert inquiry.next_followup_at == target


def test_won_closes_and_clears_schedule(db, settings, channels, make_user):
    rep = make_user()
    inquiry = _create(db, settings, channels[0])
    svc.log_contact(
        db, inquiry, rep, ActivityKind.VISIT, ContactOutcome.WON, None, settings, utcnow()
    )
    assert inquiry.status == InquiryStatus.WON
    assert inquiry.next_followup_at is None
    assert inquiry.closed_at is not None
    with pytest.raises(svc.DomainError):
        svc.log_contact(
            db, inquiry, rep, ActivityKind.CALL, ContactOutcome.REACHED, None, settings, utcnow()
        )


def test_lost_requires_reason(db, settings, channels, make_user):
    rep = make_user()
    inquiry = _create(db, settings, channels[0])
    with pytest.raises(svc.DomainError) as exc:
        svc.log_contact(
            db, inquiry, rep, ActivityKind.CALL, ContactOutcome.LOST, None, settings, utcnow()
        )
    assert "lost_reason" in exc.value.errors
    svc.log_contact(
        db,
        inquiry,
        rep,
        ActivityKind.CALL,
        ContactOutcome.LOST,
        None,
        settings,
        utcnow(),
        lost_reason=LostReason.PRICE,
    )
    assert inquiry.status == InquiryStatus.LOST
    assert inquiry.lost_reason == LostReason.PRICE


def test_cadence_exhaustion_leaves_inquiry_needing_decision(db, settings, channels, make_user):
    rep = make_user()
    inquiry = _create(db, settings, channels[0], timeframe=Timeframe.JUST_CHECKING)
    for _ in range(3):
        svc.log_contact(
            db, inquiry, rep, ActivityKind.CALL, ContactOutcome.NO_ANSWER, None, settings, utcnow()
        )
    db.commit()
    assert inquiry.is_open and inquiry.next_followup_at is None
    queues = svc.followup_queues(db, None, settings, utcnow())
    assert [i.id for i in queues.needs_decision] == [inquiry.id]


def test_reopen_restarts_cadence(db, settings, channels, make_user):
    rep = make_user()
    inquiry = _create(db, settings, channels[0])
    svc.log_contact(
        db,
        inquiry,
        rep,
        ActivityKind.CALL,
        ContactOutcome.LOST,
        None,
        settings,
        utcnow(),
        lost_reason=LostReason.JUST_BROWSING,
    )
    svc.reopen(db, inquiry, rep, settings, utcnow())
    assert inquiry.status == InquiryStatus.CONTACTED
    assert inquiry.followup_count == 0
    assert inquiry.next_followup_at is not None
    assert inquiry.lost_reason is None


def test_note_does_not_count_as_contact(db, settings, channels, make_user):
    rep = make_user()
    inquiry = _create(db, settings, channels[0])
    svc.log_contact(db, inquiry, rep, ActivityKind.NOTE, None, "Asked for blue", settings, utcnow())
    assert inquiry.followup_count == 0
    assert inquiry.first_response_at is None


def test_followup_queues_split_by_due_time(db, settings, channels, make_user):
    rep = make_user()
    now = utcnow()
    overdue = _create(db, settings, channels[0], phone="0771111111")
    overdue.next_followup_at = now - timedelta(hours=3)
    later = _create(db, settings, channels[0], phone="0772222222")
    later.next_followup_at = now + timedelta(days=3)
    later.assigned_to = rep
    db.commit()
    everyone = svc.followup_queues(db, None, settings, now)
    assert [i.id for i in everyone.overdue] == [overdue.id]
    assert [i.id for i in everyone.upcoming] == [later.id]
    mine = svc.followup_queues(db, rep.id, settings, now)
    assert mine.overdue == [] and [i.id for i in mine.upcoming] == [later.id]
    unassigned = svc.followup_queues(db, 0, settings, now)
    assert [i.id for i in unassigned.overdue] == [overdue.id]


def test_update_changes_are_audited(db, settings, channels, make_user):
    coord = make_user(Role.COORDINATOR)
    rep = make_user(Role.SALES)
    inquiry = _create(db, settings, channels[0])
    clean = svc.clean_inquiry_input(
        db,
        svc.InquiryInput(
            phone="0771234567",
            channel_id=channels[1].id,
            product_text="iPhone 16 Pro",
            timeframe=Timeframe.TODAY,
            assigned_to_id=rep.id,
        ),
        settings,
    )
    changes = svc.update_inquiry(db, inquiry, clean, coord, settings, utcnow())
    db.commit()
    assert set(changes) == {"source", "time period", "salesperson"}
    kinds = {a.kind for a in inquiry.activities}
    assert {ActivityKind.EDIT, ActivityKind.ASSIGNMENT} <= kinds
    assert inquiry.assigned_to_id == rep.id


# ---------------------------------------------------------------- specific purchase date
def _input(channel, **kw):
    return svc.InquiryInput(
        phone="0771234567", channel_id=channel.id, product_text="iPhone 17", **kw
    )


def test_specific_date_is_required_and_validated(db, settings, channels):
    today = utcnow().astimezone(settings.tz).date()
    cases = {
        "": "Pick the date",
        (today - timedelta(days=1)).isoformat(): "can't be in the past",
        (today + timedelta(days=400)).isoformat(): "within the next 12 months",
        "05/12/2026": "valid date",
    }
    for value, message in cases.items():
        with pytest.raises(svc.DomainError) as exc:
            svc.clean_inquiry_input(
                db, _input(channels[0], timeframe="on_date", planned_date=value), settings
            )
        assert message in exc.value.errors["planned_date"]


def test_specific_date_saved_and_scheduled_day_before(db, settings, channels):
    planned = utcnow().astimezone(settings.tz).date() + timedelta(days=10)
    clean = svc.clean_inquiry_input(
        db, _input(channels[0], timeframe="on_date", planned_date=planned.isoformat()), settings
    )
    inquiry = svc.create_inquiry(db, clean, None, settings, utcnow())
    db.commit()
    assert inquiry.planned_date == planned
    assert inquiry.next_followup_at.astimezone(settings.tz).date() == planned - timedelta(days=1)
    assert inquiry.timeframe_label.startswith("Buys ")


def test_date_ignored_for_other_time_periods(db, settings, channels):
    planned = utcnow().astimezone(settings.tz).date() + timedelta(days=10)
    clean = svc.clean_inquiry_input(
        db, _input(channels[0], timeframe="this_week", planned_date=planned.isoformat()), settings
    )
    assert clean.planned_date is None


def test_existing_past_date_can_be_kept_when_editing(db, settings, channels):
    past = utcnow().astimezone(settings.tz).date() - timedelta(days=3)
    clean = svc.clean_inquiry_input(
        db,
        _input(channels[0], timeframe="on_date", planned_date=past.isoformat()),
        settings,
        keep_planned_date=past,
    )
    assert clean.planned_date == past


def test_changing_the_date_reschedules(db, settings, channels, make_user):
    coord = make_user(Role.COORDINATOR)
    inquiry = _create(db, settings, channels[0])
    planned = utcnow().astimezone(settings.tz).date() + timedelta(days=20)
    clean = svc.clean_inquiry_input(
        db, _input(channels[0], timeframe="on_date", planned_date=planned.isoformat()), settings
    )
    changes = svc.update_inquiry(db, inquiry, clean, coord, settings, utcnow())
    assert {"time period", "purchase date"} <= set(changes)
    assert inquiry.next_followup_at.astimezone(settings.tz).date() == planned - timedelta(days=1)

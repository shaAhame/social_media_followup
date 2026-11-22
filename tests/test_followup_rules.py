from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.models import Timeframe
from app.services.followup import BusinessHours, local_day_bounds, next_followup_at

TZ = ZoneInfo("Asia/Colombo")
HOURS = BusinessHours(TZ, 10, 20)


def local(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=TZ)


def test_today_lead_followed_up_two_hours_later():
    now = local(2026, 11, 20, 11, 15)
    assert next_followup_at(Timeframe.TODAY, 0, now, HOURS) == local(2026, 11, 20, 13, 15)


def test_today_lead_logged_in_evening_moves_to_next_morning():
    now = local(2026, 11, 20, 19, 0)  # +2h = 21:00, after closing
    assert next_followup_at(Timeframe.TODAY, 0, now, HOURS) == local(2026, 11, 21, 10, 0)


def test_today_lead_logged_before_opening_moves_to_opening():
    now = local(2026, 11, 20, 6, 0)  # +2h = 08:00, before opening
    assert next_followup_at(Timeframe.TODAY, 0, now, HOURS) == local(2026, 11, 20, 10, 0)


@pytest.mark.parametrize(
    ("timeframe", "attempts", "expected_day"),
    [
        (Timeframe.THIS_WEEK, 0, 21),
        (Timeframe.THIS_WEEK, 1, 22),
        (Timeframe.THIS_MONTH, 0, 23),
        (Timeframe.JUST_CHECKING, 0, 27),
        (Timeframe.TODAY, 1, 21),
    ],
)
def test_day_based_followups_land_at_start_of_business_day(timeframe, attempts, expected_day):
    now = local(2026, 11, 20, 15, 42)
    assert next_followup_at(timeframe, attempts, now, HOURS) == local(2026, 11, expected_day, 10)


def test_cadence_exhausts():
    now = local(2026, 11, 20, 12)
    assert next_followup_at(Timeframe.JUST_CHECKING, 3, now, HOURS) is None
    assert next_followup_at(Timeframe.THIS_WEEK, 4, now, HOURS) is None
    assert next_followup_at(Timeframe.THIS_WEEK, 2, now, HOURS, max_attempts=2) is None


def test_result_is_utc():
    result = next_followup_at(Timeframe.THIS_WEEK, 0, local(2026, 11, 20, 12), HOURS)
    assert result.tzinfo == UTC


def test_local_day_bounds():
    start, end = local_day_bounds(local(2026, 11, 20, 23, 59), TZ)
    assert start == local(2026, 11, 20, 0)
    assert end == local(2026, 11, 21, 0)


# ---------------------------------------------------------------- specific date ("Pick a date")
def test_specific_date_reminds_the_day_before():
    from datetime import date

    now = local(2026, 11, 20, 15, 0)
    due = next_followup_at(Timeframe.ON_DATE, 0, now, HOURS, planned_date=date(2026, 12, 5))
    assert due == local(2026, 12, 4, 10, 0)


def test_specific_date_reminder_ignores_attempts_before_the_date():
    from datetime import date

    now = local(2026, 11, 25, 12, 0)
    due = next_followup_at(Timeframe.ON_DATE, 6, now, HOURS, planned_date=date(2026, 12, 5))
    assert due == local(2026, 12, 4, 10, 0)


@pytest.mark.parametrize("day", [20, 21])  # today or tomorrow
def test_specific_date_close_by_is_followed_up_within_hours(day):
    from datetime import date

    now = local(2026, 11, 20, 11, 0)
    due = next_followup_at(Timeframe.ON_DATE, 0, now, HOURS, planned_date=date(2026, 11, day))
    assert due == local(2026, 11, 20, 13, 0)


def test_specific_date_after_reminder_uses_short_cadence():
    from datetime import date

    now = local(2026, 12, 4, 10, 30)  # the reminder day, after one contact
    due = next_followup_at(Timeframe.ON_DATE, 1, now, HOURS, planned_date=date(2026, 12, 5))
    assert due == local(2026, 12, 5, 10, 0)


def test_specific_date_without_date_behaves_like_this_week():
    now = local(2026, 11, 20, 15, 0)
    assert next_followup_at(Timeframe.ON_DATE, 0, now, HOURS) == next_followup_at(
        Timeframe.THIS_WEEK, 0, now, HOURS
    )

"""Follow-up scheduling rules.

The customer's buying time period decides how soon and how often we follow up.
Each list is the gap before the 1st, 2nd, 3rd ... follow-up attempt. After the last
step (or ``max_attempts``) no further date is scheduled and the inquiry shows up
under "Needs decision" so a person closes it as sold/lost or sets a date manually.

Day-based follow-ups land at the start of the business day (10:00 by default) so
each salesperson gets one clean call list every morning. Hour-based follow-ups are
pushed into business hours.

Specific date (``Timeframe.ON_DATE``): the customer named the day they will buy.
Until then the follow-up is the morning of the day *before* that date (confirm the
visit, reserve stock). From that day on the "This week" cadence applies.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.models import Timeframe

CADENCE: dict[Timeframe, tuple[timedelta, ...]] = {
    Timeframe.TODAY: (
        timedelta(hours=2),
        timedelta(days=1),
        timedelta(days=2),
        timedelta(days=4),
    ),
    Timeframe.THIS_WEEK: (
        timedelta(days=1),
        timedelta(days=2),
        timedelta(days=4),
        timedelta(days=7),
    ),
    Timeframe.THIS_MONTH: (
        timedelta(days=3),
        timedelta(days=7),
        timedelta(days=7),
        timedelta(days=14),
    ),
    Timeframe.JUST_CHECKING: (
        timedelta(days=7),
        timedelta(days=14),
        timedelta(days=30),
    ),
    # Used once the customer's chosen date is close (tomorrow or sooner) or has passed.
    Timeframe.ON_DATE: (
        timedelta(hours=2),
        timedelta(days=1),
        timedelta(days=2),
        timedelta(days=4),
    ),
}

# Lower number = more urgent. Used for sorting call lists.
PRIORITY: dict[Timeframe, int] = {
    Timeframe.TODAY: 1,
    Timeframe.THIS_WEEK: 2,
    Timeframe.THIS_MONTH: 3,
    Timeframe.JUST_CHECKING: 4,
    Timeframe.ON_DATE: 2,
}


@dataclass(frozen=True)
class BusinessHours:
    tz: ZoneInfo
    start_hour: int = 10
    end_hour: int = 20

    def clamp(self, moment: datetime) -> datetime:
        """Move ``moment`` into business hours (same day if early, next day if late)."""
        local = moment.astimezone(self.tz)
        if local.hour < self.start_hour:
            local = datetime.combine(local.date(), time(self.start_hour), self.tz)
        elif local.hour >= self.end_hour:
            local = datetime.combine(
                local.date() + timedelta(days=1), time(self.start_hour), self.tz
            )
        return local.astimezone(UTC)

    def start_of_day(self, moment: datetime) -> datetime:
        local = moment.astimezone(self.tz)
        return datetime.combine(local.date(), time(self.start_hour), self.tz).astimezone(UTC)


def next_followup_at(
    timeframe: Timeframe,
    attempts_done: int,
    now: datetime,
    hours: BusinessHours,
    max_attempts: int = 4,
    planned_date: date | None = None,
) -> datetime | None:
    """When the next follow-up is due, or ``None`` when the cadence is exhausted."""
    if attempts_done < 0:
        raise ValueError("attempts_done must be >= 0")
    if timeframe == Timeframe.ON_DATE:
        if planned_date is None:
            timeframe = Timeframe.THIS_WEEK
        else:
            day_before = planned_date - timedelta(days=1)
            if now.astimezone(hours.tz).date() < day_before:
                # Still ahead of the customer's date: remind the day before it.
                return datetime.combine(day_before, time(hours.start_hour), hours.tz).astimezone(
                    UTC
                )
    steps = CADENCE[timeframe]
    if attempts_done >= min(max_attempts, len(steps)):
        return None
    gap = steps[attempts_done]
    candidate = now + gap
    if gap >= timedelta(days=1):
        return hours.start_of_day(candidate)
    return hours.clamp(candidate)


def local_day_bounds(now: datetime, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """UTC start/end of the local calendar day containing ``now``."""
    local_date = now.astimezone(tz).date()
    start = datetime.combine(local_date, time.min, tz)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)

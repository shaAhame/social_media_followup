"""Everything the home dashboard shows, computed in one place.

Sales staff see their own leads; coordinators, managers and admins see everyone.
"""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.config import Settings
from app.models import (
    OPEN_STATUSES,
    Inquiry,
    InquiryStatus,
    PushSubscription,
    Role,
    Timeframe,
    User,
)
from app.services.followup import local_day_bounds
from app.services.inquiries import FollowupQueues, followup_queues
from app.services.reports import Report, build_report

PIPELINE_STAGES = (
    InquiryStatus.NEW,
    InquiryStatus.CONTACTED,
    InquiryStatus.PRICE_SENT,
    InquiryStatus.VISIT_PLANNED,
)


@dataclass
class DayBar:
    day: date
    inquiries: int
    sold: int


@dataclass
class TeamRow:
    user: User
    open: int = 0
    overdue: int = 0
    due_today: int = 0
    sold_month: int = 0
    total_month: int = 0
    has_device: bool = False

    @property
    def conversion(self) -> float:
        return round(100 * self.sold_month / self.total_month, 1) if self.total_month else 0.0


@dataclass
class Dashboard:
    scoped: bool  # True = the user's own leads only
    queues: FollowupQueues
    done_today: int
    new_today: int
    open_total: int
    unassigned: int
    month_total: int
    month_won: int
    month_lost: int
    median_first_response_min: float | None
    hot_leads: list[Inquiry]
    pipeline: list[tuple[InquiryStatus, int]]
    trend: list[DayBar]
    team: list[TeamRow] = field(default_factory=list)
    month_report: Report | None = None
    latest: list[Inquiry] = field(default_factory=list)

    @property
    def to_contact(self) -> int:
        return len(self.queues.overdue) + len(self.queues.due_today)

    @property
    def today_total(self) -> int:
        return self.done_today + self.to_contact

    @property
    def progress(self) -> float:
        """Share of today's follow-up work already done (0-1)."""
        return self.done_today / self.today_total if self.today_total else 1.0

    @property
    def month_conversion(self) -> float:
        return round(100 * self.month_won / self.month_total, 1) if self.month_total else 0.0

    @property
    def pipeline_open(self) -> int:
        return sum(count for _, count in self.pipeline)

    @property
    def trend_max(self) -> int:
        return max((bar.inquiries for bar in self.trend), default=0)


def _count(db: Session, *conditions) -> int:
    return db.scalar(select(func.count()).select_from(Inquiry).where(*conditions)) or 0


def build_dashboard(db: Session, user: User, settings: Settings, now: datetime) -> Dashboard:
    tz = settings.tz
    scoped = not user.can_coordinate
    mine = (Inquiry.assigned_to_id == user.id,) if scoped else ()
    start_today, end_today = local_day_bounds(now, tz)
    today = now.astimezone(tz).date()
    month_start = datetime.combine(today.replace(day=1), time.min, tz)
    is_open = Inquiry.status.in_(OPEN_STATUSES)

    queues = followup_queues(db, user.id if scoped else None, settings, now, upcoming_days=7)

    # Follow-up work done today = open/closed leads someone contacted today
    done_today = _count(db, *mine, Inquiry.last_contacted_at >= start_today)

    month_won = _count(
        db, *mine, Inquiry.created_at >= month_start, Inquiry.status == InquiryStatus.WON
    )
    month_lost = _count(
        db, *mine, Inquiry.created_at >= month_start, Inquiry.status == InquiryStatus.LOST
    )

    week_ago = now - timedelta(days=7)
    response_pairs = db.execute(
        select(Inquiry.created_at, Inquiry.first_response_at).where(
            *mine, Inquiry.created_at >= week_ago, Inquiry.first_response_at.is_not(None)
        )
    ).all()
    minutes = [max((first - created).total_seconds() / 60, 0) for created, first in response_pairs]

    # Hot leads: buying today, or a specific date within the next 7 days
    hot_leads = list(
        db.scalars(
            select(Inquiry)
            .options(
                joinedload(Inquiry.customer),
                joinedload(Inquiry.channel),
                joinedload(Inquiry.assigned_to),
            )
            .where(
                *mine,
                is_open,
                or_(
                    Inquiry.timeframe == Timeframe.TODAY,
                    and_(
                        Inquiry.timeframe == Timeframe.ON_DATE,
                        Inquiry.planned_date <= today + timedelta(days=7),
                    ),
                ),
            )
            .order_by(Inquiry.planned_date.is_(None), Inquiry.planned_date, Inquiry.created_at)
            .limit(8)
        )
    )

    stage_counts = dict(
        db.execute(
            select(Inquiry.status, func.count()).where(*mine, is_open).group_by(Inquiry.status)
        ).all()
    )
    pipeline = [(stage, stage_counts.get(stage, 0)) for stage in PIPELINE_STAGES]

    # 14-day trend (local days): inquiries logged, and how many of those days' leads sold
    trend_start = datetime.combine(today - timedelta(days=13), time.min, tz)
    created = Counter()
    sold = Counter()
    for created_at, status in db.execute(
        select(Inquiry.created_at, Inquiry.status).where(*mine, Inquiry.created_at >= trend_start)
    ):
        day = created_at.astimezone(tz).date()
        created[day] += 1
        if status == InquiryStatus.WON:
            sold[day] += 1
    trend = [
        DayBar(d, created.get(d, 0), sold.get(d, 0))
        for d in (today - timedelta(days=13 - i) for i in range(14))
    ]

    dashboard = Dashboard(
        scoped=scoped,
        queues=queues,
        done_today=done_today,
        new_today=_count(db, *mine, Inquiry.created_at >= start_today),
        open_total=_count(db, *mine, is_open),
        unassigned=_count(db, is_open, Inquiry.assigned_to_id.is_(None)),
        month_total=_count(db, *mine, Inquiry.created_at >= month_start),
        month_won=month_won,
        month_lost=month_lost,
        median_first_response_min=round(statistics.median(minutes), 1) if minutes else None,
        hot_leads=hot_leads,
        pipeline=pipeline,
        trend=trend,
    )
    if scoped:
        return dashboard

    dashboard.team = build_team(db, settings, now, month_start, end_today)
    dashboard.month_report = build_report(db, today.replace(day=1), today, settings, now)
    dashboard.latest = list(
        db.scalars(
            select(Inquiry)
            .options(
                joinedload(Inquiry.customer),
                joinedload(Inquiry.channel),
                joinedload(Inquiry.assigned_to),
            )
            .order_by(Inquiry.created_at.desc())
            .limit(8)
        )
    )
    return dashboard


def build_team(
    db: Session, settings: Settings, now: datetime, month_start: datetime, end_today: datetime
) -> list[TeamRow]:
    users = list(db.scalars(select(User).where(User.is_active.is_(True)).order_by(User.full_name)))
    rows = {u.id: TeamRow(user=u) for u in users}
    is_open = Inquiry.status.in_(OPEN_STATUSES)
    owner = Inquiry.assigned_to_id

    def grouped(*conditions):
        return db.execute(
            select(owner, func.count()).where(owner.is_not(None), *conditions).group_by(owner)
        ).all()

    for uid, n in grouped(is_open):
        if uid in rows:
            rows[uid].open = n
    for uid, n in grouped(is_open, Inquiry.next_followup_at < now):
        if uid in rows:
            rows[uid].overdue = n
    for uid, n in grouped(
        is_open, Inquiry.next_followup_at >= now, Inquiry.next_followup_at < end_today
    ):
        if uid in rows:
            rows[uid].due_today = n
    for uid, n in grouped(Inquiry.created_at >= month_start):
        if uid in rows:
            rows[uid].total_month = n
    for uid, n in grouped(Inquiry.created_at >= month_start, Inquiry.status == InquiryStatus.WON):
        if uid in rows:
            rows[uid].sold_month = n
    with_device = set(db.scalars(select(PushSubscription.user_id).distinct()))
    for uid, row in rows.items():
        row.has_device = uid in with_device
    # Sales staff always listed; others only if they hold leads
    team = [r for r in rows.values() if r.user.role == Role.SALES or r.open or r.total_month]
    return sorted(team, key=lambda r: (-r.overdue, -r.open, r.user.full_name))

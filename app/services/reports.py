"""Management reports: where leads come from, who converts them, and why we lose them."""

from __future__ import annotations

import csv
import io
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import OPEN_STATUSES, Channel, Inquiry, InquiryStatus, LostReason, Timeframe, User
from app.services.phone import display_phone


@dataclass
class ReportRow:
    label: str
    total: int = 0
    won: int = 0
    lost: int = 0
    open: int = 0

    @property
    def conversion(self) -> float:
        return round(100 * self.won / self.total, 1) if self.total else 0.0

    def add(self, status: InquiryStatus, count: int) -> None:
        self.total += count
        if status == InquiryStatus.WON:
            self.won += count
        elif status == InquiryStatus.LOST:
            self.lost += count
        else:
            self.open += count


@dataclass
class UserRow(ReportRow):
    overdue_now: int = 0
    median_first_response_min: float | None = None


@dataclass
class Report:
    start: date
    end: date
    summary: ReportRow
    by_channel: list[ReportRow]
    by_user: list[UserRow]
    by_timeframe: list[ReportRow]
    top_products: list[ReportRow]
    lost_reasons: list[tuple[str, int]]
    daily: list[tuple[date, int]]
    median_first_response_min: float | None
    pct_responded_within_15_min: float | None
    max_daily: int = field(default=0)


def period_bounds(start: date, end: date, settings: Settings) -> tuple[datetime, datetime]:
    tz = settings.tz
    return (
        datetime.combine(start, time.min, tz),
        datetime.combine(end + timedelta(days=1), time.min, tz),
    )


def _pivot(
    rows: Iterable[tuple[str | None, InquiryStatus, int]], none_label: str
) -> list[ReportRow]:
    table: dict[str, ReportRow] = {}
    for key, status, count in rows:
        label = key if key is not None else none_label
        table.setdefault(label, ReportRow(label)).add(status, count)
    return sorted(table.values(), key=lambda r: r.total, reverse=True)


def build_report(db: Session, start: date, end: date, settings: Settings, now: datetime) -> Report:
    start_utc, end_utc = period_bounds(start, end, settings)
    in_period = (Inquiry.created_at >= start_utc, Inquiry.created_at < end_utc)

    summary = ReportRow("All sources")
    for status, count in db.execute(
        select(Inquiry.status, func.count()).where(*in_period).group_by(Inquiry.status)
    ):
        summary.add(status, count)

    by_channel = _pivot(
        db.execute(
            select(Channel.name, Inquiry.status, func.count())
            .join(Inquiry.channel)
            .where(*in_period)
            .group_by(Channel.name, Inquiry.status)
        ),
        "Unknown",
    )

    timeframe_rows = db.execute(
        select(Inquiry.timeframe, Inquiry.status, func.count())
        .where(*in_period)
        .group_by(Inquiry.timeframe, Inquiry.status)
    ).all()
    by_timeframe_map = {t: ReportRow(t.label) for t in Timeframe}
    for tf, status, count in timeframe_rows:
        by_timeframe_map[tf].add(status, count)
    by_timeframe = [r for r in by_timeframe_map.values() if r.total]

    product_rows = db.execute(
        select(
            func.lower(Inquiry.product_text),
            func.min(Inquiry.product_text),
            Inquiry.status,
            func.count(),
        )
        .where(*in_period)
        .group_by(func.lower(Inquiry.product_text), Inquiry.status)
    ).all()
    products: dict[str, ReportRow] = {}
    for key, label, status, count in product_rows:
        products.setdefault(key, ReportRow(label)).add(status, count)
    top_products = sorted(products.values(), key=lambda r: r.total, reverse=True)[:15]

    lost_reasons = [
        (reason.label if reason else "Not recorded", count)
        for reason, count in db.execute(
            select(Inquiry.lost_reason, func.count())
            .where(*in_period, Inquiry.status == InquiryStatus.LOST)
            .group_by(Inquiry.lost_reason)
            .order_by(func.count().desc())
        )
    ]

    # Per-salesperson performance (leads created in the period, by current owner)
    users: dict[int | None, UserRow] = {}
    names = dict(db.execute(select(User.id, User.full_name)).all())
    for user_id, status, count in db.execute(
        select(Inquiry.assigned_to_id, Inquiry.status, func.count())
        .where(*in_period)
        .group_by(Inquiry.assigned_to_id, Inquiry.status)
    ):
        users.setdefault(user_id, UserRow(names.get(user_id, "Unassigned"))).add(status, count)
    for user_id, count in db.execute(
        select(Inquiry.assigned_to_id, func.count())
        .where(Inquiry.status.in_(OPEN_STATUSES), Inquiry.next_followup_at < now)
        .group_by(Inquiry.assigned_to_id)
    ):
        users.setdefault(user_id, UserRow(names.get(user_id, "Unassigned"))).overdue_now = count

    # Speed of first response (logged time -> first contact attempt)
    response_rows = db.execute(
        select(Inquiry.assigned_to_id, Inquiry.created_at, Inquiry.first_response_at).where(
            *in_period, Inquiry.first_response_at.is_not(None)
        )
    ).all()
    per_user_minutes: dict[int | None, list[float]] = defaultdict(list)
    all_minutes: list[float] = []
    for user_id, created, first in response_rows:
        minutes = max((first - created).total_seconds() / 60, 0)
        per_user_minutes[user_id].append(minutes)
        all_minutes.append(minutes)
    for user_id, minutes in per_user_minutes.items():
        if user_id in users:
            users[user_id].median_first_response_min = round(statistics.median(minutes), 1)
    by_user = sorted(users.values(), key=lambda r: (r.won, r.total), reverse=True)

    # Daily volume, bucketed by local date
    counts = Counter(
        created.astimezone(settings.tz).date()
        for created in db.scalars(select(Inquiry.created_at).where(*in_period))
    )
    daily = []
    day = start
    while day <= end:
        daily.append((day, counts.get(day, 0)))
        day += timedelta(days=1)

    return Report(
        start=start,
        end=end,
        summary=summary,
        by_channel=by_channel,
        by_user=by_user,
        by_timeframe=by_timeframe,
        top_products=top_products,
        lost_reasons=lost_reasons,
        daily=daily,
        max_daily=max((c for _, c in daily), default=0),
        median_first_response_min=round(statistics.median(all_minutes), 1) if all_minutes else None,
        pct_responded_within_15_min=(
            round(100 * sum(m <= 15 for m in all_minutes) / len(all_minutes), 1)
            if all_minutes
            else None
        ),
    )


CSV_HEADERS = [
    "inquiry_id",
    "created_at",
    "customer_name",
    "contact_number",
    "source",
    "product",
    "time_period",
    "status",
    "payment_type",
    "budget_lkr",
    "assigned_to",
    "next_followup_at",
    "followups_done",
    "first_response_at",
    "lost_reason",
    "marketing_consent",
    "notes",
]


def _fmt(dt: datetime | None, settings: Settings) -> str:
    return dt.astimezone(settings.tz).strftime("%Y-%m-%d %H:%M") if dt else ""


def _safe_cell(value: str) -> str:
    # Prevent CSV/formula injection when the file is opened in Excel.
    if value and value[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def inquiries_to_csv(inquiries: Iterable[Inquiry], settings: Settings) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_HEADERS)
    for i in inquiries:
        writer.writerow(
            [
                i.id,
                _fmt(i.created_at, settings),
                _safe_cell(i.customer.name or ""),
                display_phone(i.customer.phone_e164, settings.default_phone_region),
                i.channel.name,
                _safe_cell(i.product_text),
                i.timeframe.label,
                i.status.label,
                i.payment_type.label if i.payment_type else "",
                i.budget_lkr or "",
                i.assigned_to.full_name if i.assigned_to else "",
                _fmt(i.next_followup_at, settings),
                i.followup_count,
                _fmt(i.first_response_at, settings),
                i.lost_reason.label if isinstance(i.lost_reason, LostReason) else "",
                "yes" if i.customer.marketing_consent else "no",
                _safe_cell((i.notes or "").replace("\n", " ")),
            ]
        )
    return buffer.getvalue()

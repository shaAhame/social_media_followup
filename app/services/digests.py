"""Daily notifications: the salesperson's morning call list and the manager's evening report.

A small in-app scheduler checks once a minute. Each job runs at most once per local
day: before sending, it claims a row in ``job_runs`` (job, date). The primary key makes
the claim atomic, so restarts, redeploys or several workers never double-send. If the
app was down at the scheduled time, the job still runs if it comes back within
``CATCH_UP`` (2 hours).
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import date, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.config import Settings, get_settings
from app.db import SessionLocal, get_engine, utcnow
from app.models import OPEN_STATUSES, Inquiry, InquiryStatus, JobRun, Role, User
from app.services.followup import local_day_bounds
from app.services.inquiries import followup_queues
from app.services.push import Notification, send_to_user

log = logging.getLogger(__name__)

MORNING = "morning_digest"
EVENING = "evening_report"
CATCH_UP = timedelta(hours=2)
REPORT_ROLES = (Role.ADMIN, Role.MANAGER, Role.COORDINATOR)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


# ---------------------------------------------------------------- message builders
def build_morning_digest(db: Session, user: User, settings: Settings, now: datetime):
    """Call list for one person, or None when there is nothing to do."""
    queues = followup_queues(db, user.id, settings, now, upcoming_days=0)
    overdue, due = len(queues.overdue), len(queues.due_today)
    unassigned = 0
    if user.can_coordinate:
        unassigned = (
            db.scalar(
                select(func.count())
                .select_from(Inquiry)
                .where(Inquiry.status.in_(OPEN_STATUSES), Inquiry.assigned_to_id.is_(None))
            )
            or 0
        )
    if overdue + due + unassigned == 0:
        return None

    parts = []
    if overdue:
        parts.append(f"{overdue} overdue")
    if due:
        parts.append(f"{due} due today")
    if unassigned:
        parts.append(f"{_plural(unassigned, 'unassigned lead')} to share out")
    body = " · ".join(parts) + "."
    first = (queues.overdue + queues.due_today)[:1]
    if first:
        lead = first[0]
        body += f" First: {lead.customer.name or 'customer'} – {lead.product_text}."

    total = overdue + due
    if total:
        title = f"Good morning {user.first_name}: {_plural(total, 'follow-up')} today"
        url = "/followups?owner=me"
    else:
        title = f"Good morning {user.first_name}: leads waiting to be assigned"
        url = "/inquiries?assigned=none"
    return Notification(title=title, body=body, url=url, tag=MORNING)


def build_evening_report(db: Session, settings: Settings, now: datetime) -> Notification:
    start, end = local_day_bounds(now, settings.tz)
    today = (Inquiry.created_at >= start, Inquiry.created_at < end)
    new = db.scalar(select(func.count()).select_from(Inquiry).where(*today)) or 0
    closed_today = (Inquiry.closed_at >= start, Inquiry.closed_at < end)
    sold = (
        db.scalar(
            select(func.count())
            .select_from(Inquiry)
            .where(*closed_today, Inquiry.status == InquiryStatus.WON)
        )
        or 0
    )
    lost_rows = db.scalars(
        select(Inquiry.lost_reason).where(*closed_today, Inquiry.status == InquiryStatus.LOST)
    ).all()
    overdue_rows = db.execute(
        select(User.full_name, func.count())
        .select_from(Inquiry)
        .outerjoin(User, Inquiry.assigned_to_id == User.id)
        .where(Inquiry.status.in_(OPEN_STATUSES), Inquiry.next_followup_at < now)
        .group_by(User.full_name)
        .order_by(func.count().desc())
    ).all()
    unassigned = (
        db.scalar(
            select(func.count())
            .select_from(Inquiry)
            .where(Inquiry.status.in_(OPEN_STATUSES), Inquiry.assigned_to_id.is_(None))
        )
        or 0
    )

    title = f"Today: {new} {'inquiry' if new == 1 else 'inquiries'} · {sold} sold"
    if lost_rows:
        title += f" · {len(lost_rows)} lost"
    overdue_total = sum(count for _, count in overdue_rows)
    lines = []
    if overdue_total:
        top = ", ".join(
            f"{(name or 'Unassigned').split()[0]} {count}" for name, count in overdue_rows[:3]
        )
        lines.append(f"Overdue now: {overdue_total} ({top}).")
    else:
        lines.append("No overdue follow-ups. 👏")
    if unassigned:
        lines.append(f"Unassigned: {unassigned}.")
    reasons = Counter(r.label for r in lost_rows if r is not None)
    if reasons:
        lines.append(f"Top lost reason: {reasons.most_common(1)[0][0]}.")
    local_day = now.astimezone(settings.tz).date().isoformat()
    return Notification(
        title=title,
        body=" ".join(lines),
        url=f"/reports?start={local_day}&end={local_day}",
        tag=EVENING,
    )


# ---------------------------------------------------------------- jobs
def _recipients(db: Session, flag) -> list[User]:
    return list(
        db.scalars(
            select(User)
            .options(selectinload(User.push_subscriptions))
            .where(User.is_active.is_(True), flag.is_(True))
        )
    )


def send_morning_digests(db: Session, settings: Settings, now: datetime) -> int:
    sent = 0
    for user in _recipients(db, User.notify_morning):
        if not user.push_subscriptions:
            continue
        note = build_morning_digest(db, user, settings, now)
        if note and send_to_user(db, user, note, settings):
            sent += 1
    db.commit()
    return sent


def send_evening_reports(db: Session, settings: Settings, now: datetime) -> int:
    recipients = [
        u
        for u in _recipients(db, User.notify_evening)
        if u.role in REPORT_ROLES and u.push_subscriptions
    ]
    if not recipients:
        return 0
    note = build_evening_report(db, settings, now)
    sent = sum(1 for user in recipients if send_to_user(db, user, note, settings))
    db.commit()
    return sent


JOBS = {
    MORNING: (lambda s: s.morning_digest_time, send_morning_digests),
    EVENING: (lambda s: s.evening_report_time, send_evening_reports),
}


# ---------------------------------------------------------------- scheduler
def parse_hhmm(value: str) -> time:
    hours, minutes = value.strip().split(":")
    return time(int(hours), int(minutes))


def due_jobs(now: datetime, settings: Settings) -> list[str]:
    local = now.astimezone(settings.tz)
    due = []
    for name, (time_of, _) in JOBS.items():
        target = datetime.combine(local.date(), parse_hhmm(time_of(settings)), settings.tz)
        if target <= local < target + CATCH_UP:
            due.append(name)
    return due


def claim(db: Session, job: str, day: date) -> bool:
    """Atomically mark ``job`` as run for ``day``. False if already claimed."""
    db.add(JobRun(job=job, run_on=day))
    try:
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        return False


def run_due_jobs(now: datetime | None = None, settings: Settings | None = None) -> dict[str, int]:
    settings = settings or get_settings()
    now = now or utcnow()
    if not settings.notifications_enabled:
        return {}
    results: dict[str, int] = {}
    get_engine()
    for name in due_jobs(now, settings):
        with SessionLocal() as db:
            day = now.astimezone(settings.tz).date()
            if not claim(db, name, day):
                continue
            try:
                sent = JOBS[name][1](db, settings, now)
            except Exception:
                db.rollback()
                log.exception("Daily job %s failed", name)
                continue
            run = db.get(JobRun, (name, day))
            if run is not None:
                run.sent = sent
                db.commit()
            results[name] = sent
            log.info("Daily job %s sent %d notification(s)", name, sent)
    return results


async def scheduler_loop() -> None:
    log.info("Notification scheduler started")
    while True:
        try:
            await asyncio.to_thread(run_due_jobs)
        except Exception:  # never let the loop die
            log.exception("Scheduler tick failed")
        await asyncio.sleep(60 - utcnow().second)

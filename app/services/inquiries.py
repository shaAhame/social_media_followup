"""Inquiry domain logic: capture, de-duplication, follow-up logging and queues.

Service functions only ``flush``; the caller (router / API / CLI) owns the
transaction and commits once, so a request is all-or-nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import TypeVar

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.config import Settings
from app.models import (
    CONTACT_KINDS,
    OPEN_STATUSES,
    Activity,
    ActivityKind,
    Channel,
    ContactOutcome,
    Customer,
    Inquiry,
    InquiryStatus,
    LostReason,
    PaymentType,
    Product,
    Timeframe,
    User,
)
from app.services.followup import PRIORITY, BusinessHours, local_day_bounds, next_followup_at
from app.services.phone import InvalidPhoneNumber, normalize_phone

E = TypeVar("E", bound=Enum)


class DomainError(ValueError):
    """Validation / business-rule failure with per-field messages for the form."""

    def __init__(self, errors: dict[str, str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors.values()))


def business_hours(settings: Settings) -> BusinessHours:
    return BusinessHours(
        settings.tz, settings.business_day_start_hour, settings.business_day_end_hour
    )


# ---------------------------------------------------------------- parsing helpers
def parse_enum(enum_cls: type[E], value, field_name: str, errors: dict, required: bool) -> E | None:
    if value in (None, ""):
        if required:
            errors[field_name] = "Please choose an option."
        return None
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(str(value))
    except ValueError:
        errors[field_name] = "Invalid choice."
        return None


_LKR_RE = re.compile(r"^(\d+(?:\.\d+)?)([km]?)$")


def parse_lkr(value, errors: dict, field_name: str = "budget_lkr") -> int | None:
    """Accepts '250000', '250,000', 'Rs. 250,000', '250k', '1.2m'."""
    if value in (None, ""):
        return None
    if isinstance(value, int):
        amount = value
    else:
        text = str(value).lower().replace(",", "").replace(" ", "")
        text = re.sub(r"^(rs\.?|lkr)", "", text)
        match = _LKR_RE.match(text)
        if not match:
            errors[field_name] = "Enter an amount like 250000 or 250k."
            return None
        number = float(match.group(1))
        multiplier = {"": 1, "k": 1_000, "m": 1_000_000}[match.group(2)]
        amount = int(round(number * multiplier))
    if amount < 0:
        errors[field_name] = "Amount cannot be negative."
        return None
    return amount


def clean_text(value, max_len: int) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text[:max_len] or None


def clean_multiline(value, max_len: int = 4000) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:max_len] or None


# ---------------------------------------------------------------- input model
@dataclass
class InquiryInput:
    """Raw values as they arrive from a form or the JSON API."""

    phone: str | None
    channel_id: int | str | None
    product_text: str | None
    timeframe: str | Timeframe | None
    customer_name: str | None = None
    payment_type: str | PaymentType | None = None
    budget_lkr: str | int | None = None
    assigned_to_id: int | str | None = None
    notes: str | None = None
    marketing_consent: bool = False
    external_ref: str | None = None


@dataclass
class CleanInquiry:
    phone_e164: str
    channel: Channel
    product_text: str
    product: Product | None
    timeframe: Timeframe
    customer_name: str | None
    payment_type: PaymentType | None
    budget_lkr: int | None
    assignee: User | None
    notes: str | None
    marketing_consent: bool
    external_ref: str | None = None
    warnings: list[str] = field(default_factory=list)


def _to_int(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def clean_inquiry_input(db: Session, data: InquiryInput, settings: Settings) -> CleanInquiry:
    errors: dict[str, str] = {}

    phone_e164 = None
    try:
        phone_e164 = normalize_phone(data.phone, settings.default_phone_region)
    except InvalidPhoneNumber as exc:
        errors["phone"] = str(exc)

    channel = None
    channel_id = _to_int(data.channel_id)
    if channel_id is None:
        errors["channel_id"] = "Choose where the inquiry came from."
    else:
        channel = db.get(Channel, channel_id)
        if channel is None or not channel.is_active:
            errors["channel_id"] = "Unknown or inactive source."

    product_text = clean_text(data.product_text, 160)
    if not product_text:
        errors["product_text"] = "What product is the customer interested in?"

    timeframe = parse_enum(Timeframe, data.timeframe, "timeframe", errors, required=True)
    payment_type = parse_enum(
        PaymentType, data.payment_type, "payment_type", errors, required=False
    )
    budget = parse_lkr(data.budget_lkr, errors)

    assignee = None
    assignee_id = _to_int(data.assigned_to_id)
    if assignee_id is not None:
        assignee = db.get(User, assignee_id)
        if assignee is None or not assignee.is_active:
            errors["assigned_to_id"] = "Unknown or inactive salesperson."

    if errors:
        raise DomainError(errors)

    product = match_product(db, product_text)
    return CleanInquiry(
        phone_e164=phone_e164,
        channel=channel,
        product_text=product.name if product else product_text,
        product=product,
        timeframe=timeframe,
        customer_name=clean_text(data.customer_name, 120),
        payment_type=payment_type,
        budget_lkr=budget,
        assignee=assignee,
        notes=clean_multiline(data.notes),
        marketing_consent=bool(data.marketing_consent),
        external_ref=clean_text(data.external_ref, 120),
    )


def match_product(db: Session, text: str) -> Product | None:
    return db.scalar(
        select(Product).where(func.lower(Product.name) == text.lower(), Product.is_active.is_(True))
    )


# ---------------------------------------------------------------- customers
def find_customer_by_phone(db: Session, phone_e164: str) -> Customer | None:
    return db.scalar(select(Customer).where(Customer.phone_e164 == phone_e164))


def find_or_create_customer(
    db: Session, phone_e164: str, name: str | None, consent: bool, now: datetime
) -> tuple[Customer, bool]:
    customer = find_customer_by_phone(db, phone_e164)
    created = customer is None
    if created:
        customer = Customer(phone_e164=phone_e164, name=name)
        db.add(customer)
    elif name and not customer.name:
        customer.name = name
    if consent and not customer.marketing_consent:
        customer.marketing_consent = True
        customer.consent_at = now
    db.flush()
    return customer, created


def open_inquiries_for_customer(db: Session, customer_id: int) -> list[Inquiry]:
    return list(
        db.scalars(
            select(Inquiry)
            .options(joinedload(Inquiry.channel), joinedload(Inquiry.assigned_to))
            .where(Inquiry.customer_id == customer_id, Inquiry.status.in_(OPEN_STATUSES))
            .order_by(Inquiry.created_at.desc())
        )
    )


# ---------------------------------------------------------------- commands
def _add_activity(
    db: Session,
    inquiry: Inquiry,
    actor: User | None,
    kind: ActivityKind,
    note: str | None = None,
    outcome: ContactOutcome | None = None,
    now: datetime | None = None,
) -> Activity:
    activity = Activity(
        inquiry=inquiry,
        user_id=actor.id if actor else None,
        kind=kind,
        outcome=outcome,
        note=note,
    )
    if now is not None:
        activity.created_at = now
    db.add(activity)
    return activity


def find_by_external_ref(db: Session, channel_id: int, external_ref: str) -> Inquiry | None:
    return db.scalar(
        select(Inquiry).where(
            Inquiry.channel_id == channel_id, Inquiry.external_ref == external_ref
        )
    )


def create_inquiry(
    db: Session, clean: CleanInquiry, actor: User | None, settings: Settings, now: datetime
) -> Inquiry:
    customer, _ = find_or_create_customer(
        db, clean.phone_e164, clean.customer_name, clean.marketing_consent, now
    )
    inquiry = Inquiry(
        customer=customer,
        channel=clean.channel,
        product=clean.product,
        product_text=clean.product_text,
        timeframe=clean.timeframe,
        status=InquiryStatus.NEW,
        payment_type=clean.payment_type,
        budget_lkr=clean.budget_lkr,
        assigned_to=clean.assignee,
        created_by_id=actor.id if actor else None,
        notes=clean.notes,
        external_ref=clean.external_ref,
        followup_count=0,
        next_followup_at=next_followup_at(
            clean.timeframe, 0, now, business_hours(settings), settings.followup_max_attempts
        ),
        created_at=now,
        updated_at=now,
    )
    db.add(inquiry)
    source = f"Logged from {clean.channel.name}"
    if clean.assignee:
        source += f", assigned to {clean.assignee.full_name}"
    _add_activity(db, inquiry, actor, ActivityKind.CREATED, source, now=now)
    db.flush()
    return inquiry


def log_contact(
    db: Session,
    inquiry: Inquiry,
    actor: User,
    kind: ActivityKind,
    outcome: ContactOutcome | None,
    note: str | None,
    settings: Settings,
    now: datetime,
    next_at: datetime | None = None,
    lost_reason: LostReason | None = None,
) -> Activity:
    """Record a follow-up (call / WhatsApp / social / visit / note) and move the pipeline."""
    errors: dict[str, str] = {}
    is_contact = kind in CONTACT_KINDS
    if kind not in (*CONTACT_KINDS, ActivityKind.NOTE):
        errors["kind"] = "Invalid activity type."
    if is_contact and outcome is None:
        errors["outcome"] = "What was the result?"
    if not inquiry.is_open and outcome is not None:
        errors["outcome"] = "This inquiry is closed. Reopen it first."
    if outcome == ContactOutcome.LOST and lost_reason is None:
        errors["lost_reason"] = "Why is the customer not buying?"
    if kind == ActivityKind.NOTE and not note:
        errors["note"] = "Write the note."
    if next_at is not None and next_at < now - timedelta(minutes=5):
        errors["next_at"] = "Next follow-up must be in the future."
    if errors:
        raise DomainError(errors)

    activity = _add_activity(db, inquiry, actor, kind, note, outcome if is_contact else None, now)
    if not is_contact:
        if next_at is not None and inquiry.is_open:
            inquiry.next_followup_at = next_at
        db.flush()
        return activity

    inquiry.followup_count += 1
    inquiry.last_contacted_at = now
    if inquiry.first_response_at is None:
        inquiry.first_response_at = now

    old_status = inquiry.status
    new_status = old_status
    if outcome == ContactOutcome.WON:
        new_status = InquiryStatus.WON
    elif outcome == ContactOutcome.LOST:
        new_status = InquiryStatus.LOST
    elif outcome == ContactOutcome.PRICE_SENT:
        new_status = InquiryStatus.PRICE_SENT
    elif outcome == ContactOutcome.VISIT_PLANNED:
        new_status = InquiryStatus.VISIT_PLANNED
    elif outcome in (ContactOutcome.REACHED, ContactOutcome.CALL_LATER) and (
        old_status == InquiryStatus.NEW
    ):
        new_status = InquiryStatus.CONTACTED

    if new_status != old_status:
        inquiry.status = new_status
        _add_activity(
            db,
            inquiry,
            actor,
            ActivityKind.STATUS_CHANGE,
            f"{old_status.label} → {new_status.label}",
            now=now,
        )

    if new_status.is_open:
        inquiry.lost_reason = None
        inquiry.next_followup_at = next_at or next_followup_at(
            inquiry.timeframe,
            inquiry.followup_count,
            now,
            business_hours(settings),
            settings.followup_max_attempts,
        )
    else:
        inquiry.closed_at = now
        inquiry.next_followup_at = None
        inquiry.lost_reason = lost_reason if new_status == InquiryStatus.LOST else None
    inquiry.updated_at = now
    db.flush()
    return activity


def assign(
    db: Session, inquiry: Inquiry, assignee: User | None, actor: User, now: datetime
) -> None:
    if inquiry.assigned_to_id == (assignee.id if assignee else None):
        return
    previous = inquiry.assigned_to.full_name if inquiry.assigned_to else "nobody"
    inquiry.assigned_to = assignee
    inquiry.updated_at = now
    target = assignee.full_name if assignee else "nobody"
    _add_activity(db, inquiry, actor, ActivityKind.ASSIGNMENT, f"{previous} → {target}", now=now)
    db.flush()


def update_inquiry(
    db: Session,
    inquiry: Inquiry,
    clean: CleanInquiry,
    actor: User,
    settings: Settings,
    now: datetime,
    status: InquiryStatus | None = None,
    lost_reason: LostReason | None = None,
) -> list[str]:
    """Apply an edit form. Returns the list of changed field labels."""
    changes: list[str] = []

    if clean.phone_e164 != inquiry.customer.phone_e164:
        customer, _ = find_or_create_customer(
            db, clean.phone_e164, clean.customer_name, clean.marketing_consent, now
        )
        inquiry.customer = customer
        changes.append("contact number")
    elif clean.customer_name and clean.customer_name != inquiry.customer.name:
        inquiry.customer.name = clean.customer_name
        changes.append("customer name")
    if clean.marketing_consent and not inquiry.customer.marketing_consent:
        inquiry.customer.marketing_consent = True
        inquiry.customer.consent_at = now
        changes.append("marketing consent")

    simple = {
        "channel": ("source", clean.channel),
        "product_text": ("product", clean.product_text),
        "payment_type": ("payment type", clean.payment_type),
        "budget_lkr": ("budget", clean.budget_lkr),
        "notes": ("notes", clean.notes),
    }
    for attr, (label, value) in simple.items():
        if getattr(inquiry, attr) != value:
            setattr(inquiry, attr, value)
            changes.append(label)
    inquiry.product = clean.product

    if clean.timeframe != inquiry.timeframe:
        inquiry.timeframe = clean.timeframe
        changes.append("time period")
        if inquiry.is_open:
            inquiry.next_followup_at = next_followup_at(
                clean.timeframe,
                inquiry.followup_count,
                now,
                business_hours(settings),
                settings.followup_max_attempts,
            )

    if status is not None and status != inquiry.status:
        if status == InquiryStatus.LOST and lost_reason is None:
            raise DomainError({"lost_reason": "Why is the customer not buying?"})
        old = inquiry.status
        inquiry.status = status
        if status.is_open:
            inquiry.closed_at = None
            inquiry.lost_reason = None
            if inquiry.next_followup_at is None:
                inquiry.next_followup_at = business_hours(settings).clamp(now + timedelta(hours=1))
        else:
            inquiry.closed_at = now
            inquiry.next_followup_at = None
            inquiry.lost_reason = lost_reason if status == InquiryStatus.LOST else None
        _add_activity(
            db, inquiry, actor, ActivityKind.STATUS_CHANGE, f"{old.label} → {status.label}", now=now
        )

    if changes:
        inquiry.updated_at = now
        _add_activity(
            db, inquiry, actor, ActivityKind.EDIT, "Changed " + ", ".join(changes), now=now
        )

    if (clean.assignee.id if clean.assignee else None) != inquiry.assigned_to_id:
        assign(db, inquiry, clean.assignee, actor, now)
        changes.append("salesperson")

    db.flush()
    return changes


def reopen(db: Session, inquiry: Inquiry, actor: User, settings: Settings, now: datetime) -> None:
    if inquiry.is_open:
        return
    old = inquiry.status
    inquiry.status = InquiryStatus.CONTACTED
    inquiry.closed_at = None
    inquiry.lost_reason = None
    inquiry.followup_count = 0  # restart the follow-up cadence
    inquiry.next_followup_at = next_followup_at(
        inquiry.timeframe, 0, now, business_hours(settings), settings.followup_max_attempts
    )
    inquiry.updated_at = now
    _add_activity(
        db,
        inquiry,
        actor,
        ActivityKind.STATUS_CHANGE,
        f"Reopened ({old.label} → {inquiry.status.label})",
        now=now,
    )
    db.flush()


# ---------------------------------------------------------------- queries
@dataclass
class InquiryFilters:
    q: str | None = None
    view: str = "open"  # open | all | won | lost | <status value>
    channel_id: int | None = None
    assigned: str | None = None  # "" (anyone) | "me" | "none" | "<user id>"
    timeframe: Timeframe | None = None
    date_from: date | None = None
    date_to: date | None = None


def _local_date_start(d: date, settings: Settings) -> datetime:
    return datetime.combine(d, time.min, settings.tz)


def filtered_inquiries(
    filters: InquiryFilters, current_user: User, settings: Settings
) -> Select[tuple[Inquiry]]:
    stmt = (
        select(Inquiry)
        .join(Inquiry.customer)
        .options(
            joinedload(Inquiry.customer),
            joinedload(Inquiry.channel),
            joinedload(Inquiry.assigned_to),
        )
    )
    if filters.view == "open":
        stmt = stmt.where(Inquiry.status.in_(OPEN_STATUSES))
    elif filters.view in {s.value for s in InquiryStatus}:
        stmt = stmt.where(Inquiry.status == InquiryStatus(filters.view))
    if filters.channel_id:
        stmt = stmt.where(Inquiry.channel_id == filters.channel_id)
    if filters.timeframe:
        stmt = stmt.where(Inquiry.timeframe == filters.timeframe)
    if filters.assigned == "me":
        stmt = stmt.where(Inquiry.assigned_to_id == current_user.id)
    elif filters.assigned == "none":
        stmt = stmt.where(Inquiry.assigned_to_id.is_(None))
    elif filters.assigned and filters.assigned.isdigit():
        stmt = stmt.where(Inquiry.assigned_to_id == int(filters.assigned))
    if filters.date_from:
        stmt = stmt.where(Inquiry.created_at >= _local_date_start(filters.date_from, settings))
    if filters.date_to:
        end = _local_date_start(filters.date_to + timedelta(days=1), settings)
        stmt = stmt.where(Inquiry.created_at < end)
    if filters.q:
        term = filters.q.strip()
        digits = re.sub(r"\D", "", term)
        conditions = [
            Customer.name.ilike(f"%{term}%"),
            Inquiry.product_text.ilike(f"%{term}%"),
        ]
        if len(digits) >= 3:
            conditions.append(Customer.phone_e164.contains(digits.lstrip("0")))
        stmt = stmt.where(or_(*conditions))
    return stmt


@dataclass
class FollowupQueues:
    overdue: list[Inquiry]
    due_today: list[Inquiry]
    upcoming: list[Inquiry]
    needs_decision: list[Inquiry]

    @property
    def total_due(self) -> int:
        return len(self.overdue) + len(self.due_today)


def followup_queues(
    db: Session, owner_id: int | None, settings: Settings, now: datetime, upcoming_days: int = 7
) -> FollowupQueues:
    """Call lists. ``owner_id=None`` = everyone; ``0`` = unassigned only."""
    _, end_of_today = local_day_bounds(now, settings.tz)
    stmt = (
        select(Inquiry)
        .options(
            joinedload(Inquiry.customer),
            joinedload(Inquiry.channel),
            joinedload(Inquiry.assigned_to),
        )
        .where(Inquiry.status.in_(OPEN_STATUSES))
    )
    if owner_id == 0:
        stmt = stmt.where(Inquiry.assigned_to_id.is_(None))
    elif owner_id is not None:
        stmt = stmt.where(Inquiry.assigned_to_id == owner_id)

    horizon = end_of_today + timedelta(days=upcoming_days)
    rows = list(
        db.scalars(
            stmt.where(
                or_(Inquiry.next_followup_at.is_(None), Inquiry.next_followup_at < horizon)
            ).order_by(Inquiry.next_followup_at)
        ).unique()
    )

    def urgency(i: Inquiry):
        return (PRIORITY[i.timeframe], i.next_followup_at or now)

    overdue = [i for i in rows if i.next_followup_at and i.next_followup_at < now]
    due_today = sorted(
        (i for i in rows if i.next_followup_at and now <= i.next_followup_at < end_of_today),
        key=urgency,
    )
    upcoming = [i for i in rows if i.next_followup_at and i.next_followup_at >= end_of_today]
    needs_decision = sorted(
        (i for i in rows if i.next_followup_at is None), key=lambda i: i.updated_at, reverse=True
    )
    return FollowupQueues(overdue, due_today, upcoming, needs_decision)


def product_suggestions(db: Session, limit: int = 60) -> list[str]:
    """Catalogue products first, then the most common free-text products."""
    names = list(
        db.scalars(select(Product.name).where(Product.is_active.is_(True)).order_by(Product.name))
    )
    seen = {n.lower() for n in names}
    popular = db.execute(
        select(Inquiry.product_text, func.count())
        .group_by(Inquiry.product_text)
        .order_by(func.count().desc())
        .limit(limit)
    ).all()
    for text, _ in popular:
        if text.lower() not in seen:
            names.append(text)
            seen.add(text.lower())
    return names

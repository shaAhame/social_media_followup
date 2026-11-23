"""ORM models.

Data model (see docs/ARCHITECTURE.md for the diagram):

* ``Customer``  - one row per real person, keyed by the normalised E.164 phone number,
                  so the same customer reaching out on a hotline and on Instagram is
                  recognised as one contact.
* ``Inquiry``   - one buying intent (product + time period) from a customer via a
                  channel. Carries the pipeline status, owner and next follow-up time.
* ``Activity``  - append-only timeline / audit trail of everything done on an inquiry.
* ``Channel``   - where the lead came from (WhatsApp hotlines, Facebook, Instagram ...).
* ``Product``   - optional catalogue used for autocomplete and clean reporting.
* ``User``      - staff accounts with a role.
* ``PushSubscription`` - a phone/browser that receives notifications for a user.
* ``AppSetting`` / ``JobRun`` - small key-value store and once-per-day job ledger.
"""

from __future__ import annotations

import enum
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, UTCDateTime, utcnow


class LabelledEnum(enum.StrEnum):
    """String enum with a human-readable label for the UI."""

    @property
    def label(self) -> str:
        # Keyed by (enum class, value): str-enums from different classes with the same
        # value compare/hash equal, so the member itself is not a safe dict key.
        return _LABELS.get((type(self).__name__, self.value), self.value.replace("_", " ").title())

    @classmethod
    def choices(cls) -> list[tuple[str, str]]:
        return [(m.value, m.label) for m in cls]


class Role(LabelledEnum):
    ADMIN = "admin"
    MANAGER = "manager"
    COORDINATOR = "coordinator"
    SALES = "sales"


class ChannelKind(LabelledEnum):
    WHATSAPP = "whatsapp"
    FACEBOOK = "facebook"
    INSTAGRAM = "instagram"
    TIKTOK = "tiktok"
    PHONE_CALL = "phone_call"
    WALK_IN = "walk_in"
    WEBSITE = "website"
    OTHER = "other"


class Timeframe(LabelledEnum):
    """When the customer intends to buy. Drives follow-up priority and cadence."""

    TODAY = "today"
    THIS_WEEK = "this_week"
    THIS_MONTH = "this_month"
    JUST_CHECKING = "just_checking"
    ON_DATE = "on_date"  # customer named a specific day; see Inquiry.planned_date


class InquiryStatus(LabelledEnum):
    NEW = "new"
    CONTACTED = "contacted"
    PRICE_SENT = "price_sent"
    VISIT_PLANNED = "visit_planned"
    WON = "won"
    LOST = "lost"

    @property
    def is_open(self) -> bool:
        return self not in (InquiryStatus.WON, InquiryStatus.LOST)


OPEN_STATUSES = tuple(s for s in InquiryStatus if s.is_open)


class PaymentType(LabelledEnum):
    CASH = "cash"
    CARD = "card"
    INSTALLMENT = "installment"
    TRADE_IN = "trade_in"
    UNDECIDED = "undecided"


class LostReason(LabelledEnum):
    PRICE = "price"
    OUT_OF_STOCK = "out_of_stock"
    BOUGHT_ELSEWHERE = "bought_elsewhere"
    NO_RESPONSE = "no_response"
    JUST_BROWSING = "just_browsing"
    OTHER = "other"


class ActivityKind(LabelledEnum):
    CREATED = "created"
    CALL = "call"
    WHATSAPP = "whatsapp"
    SOCIAL_MESSAGE = "social_message"
    VISIT = "visit"
    NOTE = "note"
    STATUS_CHANGE = "status_change"
    ASSIGNMENT = "assignment"
    EDIT = "edit"


# Activity kinds that count as a real contact attempt with the customer.
CONTACT_KINDS = (
    ActivityKind.CALL,
    ActivityKind.WHATSAPP,
    ActivityKind.SOCIAL_MESSAGE,
    ActivityKind.VISIT,
)


class ContactOutcome(LabelledEnum):
    REACHED = "reached"
    NO_ANSWER = "no_answer"
    CALL_LATER = "call_later"
    PRICE_SENT = "price_sent"
    VISIT_PLANNED = "visit_planned"
    WON = "won"
    LOST = "lost"


_LABEL_PAIRS: list[tuple[enum.Enum, str]] = [
    (ChannelKind.WHATSAPP, "WhatsApp"),
    (ChannelKind.TIKTOK, "TikTok"),
    (Timeframe.TODAY, "Today"),
    (Timeframe.THIS_WEEK, "This week"),
    (Timeframe.THIS_MONTH, "This month"),
    (Timeframe.JUST_CHECKING, "Just checking"),
    (Timeframe.ON_DATE, "Specific date"),
    (InquiryStatus.WON, "Sold"),
    (InquiryStatus.PRICE_SENT, "Price sent"),
    (InquiryStatus.VISIT_PLANNED, "Visit planned"),
    (PaymentType.TRADE_IN, "Trade-in"),
    (LostReason.PRICE, "Price too high"),
    (LostReason.OUT_OF_STOCK, "Out of stock"),
    (LostReason.BOUGHT_ELSEWHERE, "Bought elsewhere"),
    (LostReason.NO_RESPONSE, "No response"),
    (LostReason.JUST_BROWSING, "Just browsing"),
    (ActivityKind.CREATED, "Inquiry logged"),
    (ActivityKind.WHATSAPP, "WhatsApp"),
    (ActivityKind.SOCIAL_MESSAGE, "Social message"),
    (ActivityKind.STATUS_CHANGE, "Status change"),
    (ContactOutcome.REACHED, "Reached, talked"),
    (ContactOutcome.NO_ANSWER, "No answer / no reply"),
    (ContactOutcome.CALL_LATER, "Asked to contact later"),
    (ContactOutcome.PRICE_SENT, "Price / quote sent"),
    (ContactOutcome.VISIT_PLANNED, "Will visit the shop"),
    (ContactOutcome.WON, "Bought (sold)"),
    (ContactOutcome.LOST, "Not buying (lost)"),
]
_LABELS: dict[tuple[str, str], str] = {
    (type(member).__name__, member.value): text for member, text in _LABEL_PAIRS
}


def str_enum(enum_cls: type[enum.Enum]) -> Enum:
    """Store enum *values* as VARCHAR (portable, migration-friendly)."""
    return Enum(
        enum_cls,
        native_enum=False,
        length=32,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, onupdate=utcnow, nullable=False
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(str_enum(Role), default=Role.SALES)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    # Phone notifications: 9:45 call list and evening manager report
    notify_morning: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    notify_evening: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    # Instant alert when a coordinator assigns a lead to this person
    notify_assigned: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())

    push_subscriptions: Mapped[list[PushSubscription]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def first_name(self) -> str:
        return self.full_name.split()[0] if self.full_name else ""

    @property
    def can_manage(self) -> bool:
        return self.role in (Role.ADMIN, Role.MANAGER)

    @property
    def can_coordinate(self) -> bool:
        return self.role in (Role.ADMIN, Role.MANAGER, Role.COORDINATOR)

    def __repr__(self) -> str:
        return f"<User {self.email} {self.role.value}>"


class Channel(TimestampMixin, Base):
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    kind: Mapped[ChannelKind] = mapped_column(str_enum(ChannelKind))
    phone_number: Mapped[str | None] = mapped_column(String(32))
    branch: Mapped[str | None] = mapped_column(String(80))
    sort_order: Mapped[int] = mapped_column(Integer, default=100)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Product(TimestampMixin, Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    brand: Mapped[str | None] = mapped_column(String(60))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Customer(TimestampMixin, Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    phone_e164: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str | None] = mapped_column(String(120))
    # PDPA: only send promotions to customers who agreed, and keep when they agreed.
    marketing_consent: Mapped[bool] = mapped_column(Boolean, default=False)
    consent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    notes: Mapped[str | None] = mapped_column(Text)

    inquiries: Mapped[list[Inquiry]] = relationship(
        back_populates="customer", order_by="desc(Inquiry.created_at)"
    )

    @property
    def display_name(self) -> str:
        return self.name or "No name"


class Inquiry(TimestampMixin, Base):
    __tablename__ = "inquiries"
    __table_args__ = (
        CheckConstraint("budget_lkr IS NULL OR budget_lkr >= 0", name="budget_non_negative"),
        CheckConstraint("followup_count >= 0", name="followup_count_non_negative"),
        # Idempotency for automated sources (API / future Meta webhooks)
        UniqueConstraint("channel_id", "external_ref", name="uq_inquiries_channel_external_ref"),
        # Hot paths: follow-up queues filtered by status/owner and ordered by due time
        Index("ix_inquiries_status_next_followup", "status", "next_followup_at"),
        Index("ix_inquiries_assignee_next_followup", "assigned_to_id", "next_followup_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"), index=True
    )
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="RESTRICT"), index=True
    )
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"))
    product_text: Mapped[str] = mapped_column(String(160))
    timeframe: Mapped[Timeframe] = mapped_column(str_enum(Timeframe))
    # The day the customer said they will buy (only for Timeframe.ON_DATE).
    planned_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[InquiryStatus] = mapped_column(
        str_enum(InquiryStatus), default=InquiryStatus.NEW
    )
    payment_type: Mapped[PaymentType | None] = mapped_column(str_enum(PaymentType))
    budget_lkr: Mapped[int | None] = mapped_column(Integer)
    assigned_to_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    next_followup_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    followup_count: Mapped[int] = mapped_column(Integer, default=0)
    first_response_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_contacted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    lost_reason: Mapped[LostReason | None] = mapped_column(str_enum(LostReason))
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    external_ref: Mapped[str | None] = mapped_column(String(120))
    notes: Mapped[str | None] = mapped_column(Text)

    customer: Mapped[Customer] = relationship(back_populates="inquiries")
    channel: Mapped[Channel] = relationship()
    product: Mapped[Product | None] = relationship()
    assigned_to: Mapped[User | None] = relationship(foreign_keys=[assigned_to_id])
    created_by: Mapped[User | None] = relationship(foreign_keys=[created_by_id])
    activities: Mapped[list[Activity]] = relationship(
        back_populates="inquiry",
        order_by="desc(Activity.created_at), desc(Activity.id)",
        cascade="all, delete-orphan",
    )

    @property
    def is_open(self) -> bool:
        return self.status.is_open

    @property
    def timeframe_label(self) -> str:
        """'This week', or 'Buys Fri 5 Dec' when the customer gave a specific date."""
        if self.timeframe == Timeframe.ON_DATE and self.planned_date:
            return "Buys " + self.planned_date.strftime("%a %d %b").replace(" 0", " ")
        return self.timeframe.label


Index("ix_inquiries_created_at", Inquiry.created_at)


class Activity(Base):
    __tablename__ = "activities"
    __table_args__ = (Index("ix_activities_inquiry_created", "inquiry_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    inquiry_id: Mapped[int] = mapped_column(ForeignKey("inquiries.id", ondelete="CASCADE"))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    kind: Mapped[ActivityKind] = mapped_column(str_enum(ActivityKind))
    outcome: Mapped[ContactOutcome | None] = mapped_column(str_enum(ContactOutcome))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)

    inquiry: Mapped[Inquiry] = relationship(back_populates="activities")
    user: Mapped[User | None] = relationship()


class PushSubscription(Base):
    """One device (phone or browser) that has allowed notifications for a user."""

    __tablename__ = "push_subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    endpoint: Mapped[str] = mapped_column(Text, unique=True)
    p256dh: Mapped[str] = mapped_column(String(255))
    auth: Mapped[str] = mapped_column(String(255))
    device_label: Mapped[str | None] = mapped_column(String(120))
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)

    user: Mapped[User] = relationship(back_populates="push_subscriptions")


class AppSetting(Base):
    """Values the app generates and keeps itself (e.g. the notification signing keys)."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)


class JobRun(Base):
    """Ledger that makes each daily job run once per day, even across restarts/workers."""

    __tablename__ = "job_runs"

    job: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_on: Mapped[date] = mapped_column(Date, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)
    sent: Mapped[int] = mapped_column(Integer, default=0)

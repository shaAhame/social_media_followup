"""Machine endpoints: health check and lead intake API for automations.

``POST /api/v1/inquiries`` lets other systems (website forms, a future WhatsApp
Cloud API / Meta webhook bridge, Zapier/Make, a chatbot) drop leads straight into
the same queue the coordinator uses. Send ``external_ref`` (e.g. the Meta message
id) and retries are safe: the same ref on the same source returns the original
inquiry instead of creating a duplicate.
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db, utcnow
from app.models import Channel, Timeframe
from app.services import inquiries as svc

router = APIRouter()


@router.get("/healthz", include_in_schema=False)
def healthz(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"status": "ok"}


def require_api_key(x_api_key: str = Header("", alias="X-API-Key")) -> None:
    expected = get_settings().api_key
    if not expected:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "API is disabled. Set API_KEY to enable it.")
    if not secrets.compare_digest(expected, x_api_key or ""):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid API key.")


class InquiryIn(BaseModel):
    phone: str = Field(..., examples=["0771234567"])
    source: str = Field(
        ..., description="Lead source name, e.g. 'Instagram' or 'WhatsApp Hotline 2'"
    )
    product: str = Field(..., max_length=160, examples=["iPhone 16 Pro 256GB"])
    timeframe: Timeframe = Timeframe.THIS_WEEK
    customer_name: str | None = Field(None, max_length=120)
    payment_type: str | None = None
    budget_lkr: int | None = Field(None, ge=0)
    notes: str | None = Field(None, max_length=4000)
    marketing_consent: bool = False
    external_ref: str | None = Field(None, max_length=120)


class InquiryOut(BaseModel):
    id: int
    customer_id: int
    status: str
    next_followup_at: str | None
    created: bool


@router.post(
    "/api/v1/inquiries",
    response_model=InquiryOut,
    dependencies=[Depends(require_api_key)],
    tags=["api"],
)
def api_create_inquiry(body: InquiryIn, db: Session = Depends(get_db)):
    settings = get_settings()
    channel = db.scalar(select(Channel).where(func.lower(Channel.name) == body.source.lower()))
    if channel is None or not channel.is_active:
        raise HTTPException(422, f"Unknown or inactive source '{body.source}'.")

    if body.external_ref:
        existing = svc.find_by_external_ref(db, channel.id, body.external_ref)
        if existing:
            return _out(existing, created=False)

    try:
        clean = svc.clean_inquiry_input(
            db,
            svc.InquiryInput(
                phone=body.phone,
                channel_id=channel.id,
                product_text=body.product,
                timeframe=body.timeframe,
                customer_name=body.customer_name,
                payment_type=body.payment_type,
                budget_lkr=body.budget_lkr,
                notes=body.notes,
                marketing_consent=body.marketing_consent,
                external_ref=body.external_ref,
            ),
            settings,
        )
    except svc.DomainError as exc:
        raise HTTPException(422, exc.errors) from exc

    inquiry = svc.create_inquiry(db, clean, None, settings, utcnow())
    try:
        db.commit()
    except IntegrityError:
        # Concurrent retry with the same external_ref won the race.
        db.rollback()
        existing = svc.find_by_external_ref(db, channel.id, body.external_ref or "")
        if existing is None:
            raise
        return _out(existing, created=False)
    return _out(inquiry, created=True)


def _out(inquiry, created: bool) -> InquiryOut:
    return InquiryOut(
        id=inquiry.id,
        customer_id=inquiry.customer_id,
        status=inquiry.status.value,
        next_followup_at=inquiry.next_followup_at.isoformat() if inquiry.next_followup_at else None,
        created=created,
    )

"""Inquiry capture, list, detail, follow-up logging, edit, CSV export and customers."""

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.config import get_settings
from app.db import get_db, utcnow
from app.models import (
    Activity,
    ActivityKind,
    ContactOutcome,
    Customer,
    Inquiry,
    InquiryStatus,
    LostReason,
    Role,
    Timeframe,
    User,
)
from app.security import (
    can_edit_inquiry,
    get_current_user,
    require_manager,
    verify_csrf,
)
from app.services import inquiries as svc
from app.services.lookups import active_channels, active_users
from app.services.phone import display_phone, try_normalize_phone
from app.services.reports import inquiries_to_csv
from app.templating import flash, render, smartdt

router = APIRouter()
PAGE_SIZE = 50


# ---------------------------------------------------------------- helpers
def _get_inquiry(db: Session, inquiry_id: int) -> Inquiry:
    inquiry = db.scalar(
        select(Inquiry)
        .options(
            joinedload(Inquiry.customer),
            joinedload(Inquiry.channel),
            joinedload(Inquiry.assigned_to),
            selectinload(Inquiry.activities).joinedload(Activity.user),
        )
        .where(Inquiry.id == inquiry_id)
    )
    if inquiry is None:
        raise HTTPException(404, "Inquiry not found.")
    return inquiry


def _form_context(db: Session, **extra) -> dict:
    return {
        "channels": active_channels(db),
        "sales_people": active_users(db),
        "product_suggestions": svc.product_suggestions(db),
        **extra,
    }


def _parse_local_datetime(value: str | None) -> datetime | None:
    """Parse an <input type="datetime-local"> value in the business timezone."""
    if not value:
        return None
    try:
        naive = datetime.fromisoformat(value)
    except ValueError as exc:
        raise svc.DomainError({"next_at": "Invalid date/time."}) from exc
    if naive.tzinfo is not None:
        return naive
    return naive.replace(tzinfo=get_settings().tz)


def _safe_return(url: str | None) -> str:
    """Only allow local paths as post-save redirect targets."""
    if url and url.startswith("/") and not url.startswith("//") and "\\" not in url:
        return url
    return ""


def _parse_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _filters_from_query(request: Request, user: User) -> svc.InquiryFilters:
    qp = request.query_params
    default_assigned = "me" if user.role == Role.SALES else ""
    channel_id = qp.get("channel_id")
    timeframe = qp.get("timeframe")
    return svc.InquiryFilters(
        q=(qp.get("q") or "").strip() or None,
        view=qp.get("view") or "open",
        channel_id=int(channel_id) if channel_id and channel_id.isdigit() else None,
        assigned=qp.get("assigned", default_assigned),
        timeframe=Timeframe(timeframe) if timeframe in {t.value for t in Timeframe} else None,
        date_from=_parse_date(qp.get("date_from")),
        date_to=_parse_date(qp.get("date_to")),
    )


# ---------------------------------------------------------------- list & export
@router.get("/inquiries")
def list_inquiries(
    request: Request,
    page: int = Query(1, ge=1),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    settings = get_settings()
    filters = _filters_from_query(request, user)
    stmt = svc.filtered_inquiries(filters, user, settings)
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    if filters.view == "open":
        order = (Inquiry.next_followup_at.is_(None), Inquiry.next_followup_at, Inquiry.id.desc())
    else:
        order = (Inquiry.created_at.desc(),)
    rows = list(
        db.scalars(stmt.order_by(*order).offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)).unique()
    )
    return render(
        request,
        "inquiries/list.html",
        {
            "rows": rows,
            "filters": filters,
            "total": total,
            "page": page,
            "pages": max(1, -(-total // PAGE_SIZE)),
            "channels": active_channels(db),
            "sales_people": active_users(db),
        },
    )


@router.get("/inquiries/export.csv")
def export_csv(
    request: Request, user: User = Depends(require_manager), db: Session = Depends(get_db)
):
    settings = get_settings()
    filters = _filters_from_query(request, user)
    rows = db.scalars(
        svc.filtered_inquiries(filters, user, settings).order_by(Inquiry.created_at.desc())
    ).unique()
    stamp = utcnow().astimezone(settings.tz).strftime("%Y%m%d-%H%M")
    return Response(
        content="﻿" + inquiries_to_csv(rows, settings),  # BOM so Excel reads UTF-8
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="inquiries-{stamp}.csv"'},
    )


# ---------------------------------------------------------------- create
@router.get("/inquiries/new")
def new_inquiry_form(
    request: Request,
    phone: str = "",
    channel_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    values = {
        "phone": phone,
        "channel_id": channel_id or request.session.get("last_channel_id"),
        "timeframe": Timeframe.THIS_WEEK.value,
    }
    return render(request, "inquiries/form.html", _form_context(db, values=values, errors={}))


@router.post("/inquiries/new", dependencies=[Depends(verify_csrf)])
def create_inquiry(
    request: Request,
    phone: str = Form(""),
    customer_name: str = Form(""),
    channel_id: str = Form(""),
    product_text: str = Form(""),
    timeframe: str = Form(""),
    payment_type: str = Form(""),
    budget_lkr: str = Form(""),
    assigned_to_id: str = Form(""),
    notes: str = Form(""),
    marketing_consent: str | None = Form(None),
    confirm_duplicate: str | None = Form(None),
    action: str = Form("save"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    settings = get_settings()
    values = {
        "phone": phone,
        "customer_name": customer_name,
        "channel_id": channel_id,
        "product_text": product_text,
        "timeframe": timeframe,
        "payment_type": payment_type,
        "budget_lkr": budget_lkr,
        "assigned_to_id": assigned_to_id,
        "notes": notes,
        "marketing_consent": bool(marketing_consent),
    }
    raw = svc.InquiryInput(
        phone=phone,
        channel_id=channel_id,
        product_text=product_text,
        timeframe=timeframe,
        customer_name=customer_name,
        payment_type=payment_type,
        budget_lkr=budget_lkr,
        assigned_to_id=assigned_to_id,
        notes=notes,
        marketing_consent=bool(marketing_consent),
    )
    try:
        clean = svc.clean_inquiry_input(db, raw, settings)
    except svc.DomainError as exc:
        return render(
            request,
            "inquiries/form.html",
            _form_context(db, values=values, errors=exc.errors),
            status_code=422,
        )

    existing = svc.find_customer_by_phone(db, clean.phone_e164)
    if existing and not confirm_duplicate:
        open_items = svc.open_inquiries_for_customer(db, existing.id)
        if open_items:
            return render(
                request,
                "inquiries/form.html",
                _form_context(
                    db, values=values, errors={}, duplicates=open_items, existing_customer=existing
                ),
                status_code=409,
            )

    inquiry = svc.create_inquiry(db, clean, user, settings, utcnow())
    db.commit()
    request.session["last_channel_id"] = clean.channel.id
    when = smartdt(inquiry.next_followup_at) or "not scheduled"
    flash(request, f"Inquiry #{inquiry.id} saved. Next follow-up: {when}.")
    if action == "save_new":
        return RedirectResponse(f"/inquiries/new?channel_id={clean.channel.id}", status_code=303)
    return RedirectResponse(f"/inquiries/{inquiry.id}", status_code=303)


# ---------------------------------------------------------------- detail & follow-up
@router.get("/inquiries/{inquiry_id:int}")
def inquiry_detail(
    request: Request,
    inquiry_id: int,
    return_to: str = "",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    inquiry = _get_inquiry(db, inquiry_id)
    other = [
        i for i in svc.open_inquiries_for_customer(db, inquiry.customer_id) if i.id != inquiry.id
    ]
    return render(
        request,
        "inquiries/detail.html",
        {
            "inquiry": inquiry,
            "can_edit": can_edit_inquiry(user, inquiry),
            "other_open": other,
            "errors": {},
            "log_values": {},
            "return_to": _safe_return(return_to),
        },
    )


@router.post("/inquiries/{inquiry_id:int}/log", dependencies=[Depends(verify_csrf)])
def log_followup(
    request: Request,
    inquiry_id: int,
    kind: str = Form(...),
    outcome: str = Form(""),
    note: str = Form(""),
    next_at: str = Form(""),
    lost_reason: str = Form(""),
    return_to: str = Form(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    settings = get_settings()
    inquiry = _get_inquiry(db, inquiry_id)
    if not can_edit_inquiry(user, inquiry):
        raise HTTPException(403, "This inquiry belongs to another salesperson.")
    errors: dict[str, str] = {}
    kind_enum = svc.parse_enum(ActivityKind, kind, "kind", errors, required=True)
    outcome_enum = svc.parse_enum(ContactOutcome, outcome, "outcome", errors, required=False)
    reason_enum = svc.parse_enum(LostReason, lost_reason, "lost_reason", errors, required=False)
    try:
        if errors:
            raise svc.DomainError(errors)
        if inquiry.assigned_to_id is None and not user.can_coordinate:
            svc.assign(db, inquiry, user, user, utcnow())  # sales claims the lead by working it
        svc.log_contact(
            db,
            inquiry,
            user,
            kind_enum,
            outcome_enum,
            svc.clean_multiline(note),
            settings,
            utcnow(),
            next_at=_parse_local_datetime(next_at),
            lost_reason=reason_enum,
        )
    except svc.DomainError as exc:
        db.rollback()
        inquiry = _get_inquiry(db, inquiry_id)
        return render(
            request,
            "inquiries/detail.html",
            {
                "inquiry": inquiry,
                "can_edit": True,
                "other_open": [],
                "errors": exc.errors,
                "return_to": _safe_return(return_to),
                "log_values": {
                    "kind": kind,
                    "outcome": outcome,
                    "note": note,
                    "next_at": next_at,
                    "lost_reason": lost_reason,
                },
            },
            status_code=422,
        )
    db.commit()
    if inquiry.is_open:
        when = smartdt(inquiry.next_followup_at) or "none (decide: sold, lost or set a date)"
        flash(request, f"Saved. Next follow-up: {when}.")
    else:
        flash(request, f"Saved. Inquiry closed as {inquiry.status.label}.")
    return RedirectResponse(_safe_return(return_to) or f"/inquiries/{inquiry.id}", 303)


@router.post("/inquiries/{inquiry_id:int}/claim", dependencies=[Depends(verify_csrf)])
def claim(
    request: Request,
    inquiry_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    inquiry = _get_inquiry(db, inquiry_id)
    if inquiry.assigned_to_id not in (None, user.id) and not user.can_coordinate:
        raise HTTPException(403, "Already assigned to someone else.")
    svc.assign(db, inquiry, user, user, utcnow())
    db.commit()
    flash(request, "Assigned to you.")
    return RedirectResponse(f"/inquiries/{inquiry.id}", 303)


@router.post("/inquiries/{inquiry_id:int}/reopen", dependencies=[Depends(verify_csrf)])
def reopen(
    request: Request,
    inquiry_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    inquiry = _get_inquiry(db, inquiry_id)
    if not can_edit_inquiry(user, inquiry):
        raise HTTPException(403, "This inquiry belongs to another salesperson.")
    svc.reopen(db, inquiry, user, get_settings(), utcnow())
    db.commit()
    flash(request, "Inquiry reopened.")
    return RedirectResponse(f"/inquiries/{inquiry.id}", 303)


# ---------------------------------------------------------------- edit
def _edit_values(inquiry: Inquiry) -> dict:
    return {
        "phone": display_phone(inquiry.customer.phone_e164),
        "customer_name": inquiry.customer.name or "",
        "channel_id": inquiry.channel_id,
        "product_text": inquiry.product_text,
        "timeframe": inquiry.timeframe.value,
        "payment_type": inquiry.payment_type.value if inquiry.payment_type else "",
        "budget_lkr": inquiry.budget_lkr or "",
        "assigned_to_id": inquiry.assigned_to_id or "",
        "notes": inquiry.notes or "",
        "marketing_consent": inquiry.customer.marketing_consent,
        "status": inquiry.status.value,
        "lost_reason": inquiry.lost_reason.value if inquiry.lost_reason else "",
    }


@router.get("/inquiries/{inquiry_id:int}/edit")
def edit_form(
    request: Request,
    inquiry_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    inquiry = _get_inquiry(db, inquiry_id)
    if not can_edit_inquiry(user, inquiry):
        raise HTTPException(403, "This inquiry belongs to another salesperson.")
    return render(
        request,
        "inquiries/form.html",
        _form_context(db, values=_edit_values(inquiry), errors={}, inquiry=inquiry),
    )


@router.post("/inquiries/{inquiry_id:int}/edit", dependencies=[Depends(verify_csrf)])
def edit_inquiry(
    request: Request,
    inquiry_id: int,
    phone: str = Form(""),
    customer_name: str = Form(""),
    channel_id: str = Form(""),
    product_text: str = Form(""),
    timeframe: str = Form(""),
    payment_type: str = Form(""),
    budget_lkr: str = Form(""),
    assigned_to_id: str = Form(""),
    notes: str = Form(""),
    marketing_consent: str | None = Form(None),
    status: str = Form(""),
    lost_reason: str = Form(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    settings = get_settings()
    inquiry = _get_inquiry(db, inquiry_id)
    if not can_edit_inquiry(user, inquiry):
        raise HTTPException(403, "This inquiry belongs to another salesperson.")
    if not user.can_coordinate:
        # Sales staff cannot hand leads to colleagues; keep the current owner.
        assigned_to_id = str(inquiry.assigned_to_id or user.id)
    values = {
        "phone": phone,
        "customer_name": customer_name,
        "channel_id": channel_id,
        "product_text": product_text,
        "timeframe": timeframe,
        "payment_type": payment_type,
        "budget_lkr": budget_lkr,
        "assigned_to_id": assigned_to_id,
        "notes": notes,
        "marketing_consent": bool(marketing_consent),
        "status": status,
        "lost_reason": lost_reason,
    }
    errors: dict[str, str] = {}
    status_enum = svc.parse_enum(InquiryStatus, status, "status", errors, required=True)
    reason_enum = svc.parse_enum(LostReason, lost_reason, "lost_reason", errors, required=False)
    try:
        clean = svc.clean_inquiry_input(
            db,
            svc.InquiryInput(
                phone=phone,
                channel_id=channel_id,
                product_text=product_text,
                timeframe=timeframe,
                customer_name=customer_name,
                payment_type=payment_type,
                budget_lkr=budget_lkr,
                assigned_to_id=assigned_to_id,
                notes=notes,
                marketing_consent=bool(marketing_consent),
            ),
            settings,
        )
        if errors:
            raise svc.DomainError(errors)
        changes = svc.update_inquiry(
            db,
            inquiry,
            clean,
            user,
            settings,
            utcnow(),
            status=status_enum,
            lost_reason=reason_enum,
        )
    except svc.DomainError as exc:
        db.rollback()
        return render(
            request,
            "inquiries/form.html",
            _form_context(
                db,
                values=values,
                errors={**errors, **exc.errors},
                inquiry=_get_inquiry(db, inquiry_id),
            ),
            status_code=422,
        )
    db.commit()
    flash(request, "Saved." if changes else "No changes.")
    return RedirectResponse(f"/inquiries/{inquiry.id}", 303)


# ---------------------------------------------------------------- customers
@router.get("/customers/lookup")
def customer_lookup(
    phone: str = "", user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    """Live duplicate check used by the quick-entry form."""
    settings = get_settings()
    e164 = try_normalize_phone(phone, settings.default_phone_region)
    if not e164:
        return JSONResponse({"valid": False})
    customer = svc.find_customer_by_phone(db, e164)
    payload: dict = {"valid": True, "e164": e164, "display": display_phone(e164), "customer": None}
    if customer:
        payload["customer"] = {
            "id": customer.id,
            "name": customer.name,
            "total_inquiries": len(customer.inquiries),
            "open_inquiries": [
                {
                    "id": i.id,
                    "product": i.product_text,
                    "status": i.status.label,
                    "assigned_to": i.assigned_to.full_name if i.assigned_to else None,
                    "channel": i.channel.name,
                }
                for i in svc.open_inquiries_for_customer(db, customer.id)
            ],
        }
    return JSONResponse(payload)


@router.get("/customers/{customer_id:int}")
def customer_detail(
    request: Request,
    customer_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    customer = db.scalar(
        select(Customer)
        .options(
            selectinload(Customer.inquiries).joinedload(Inquiry.channel),
            selectinload(Customer.inquiries).joinedload(Inquiry.assigned_to),
        )
        .where(Customer.id == customer_id)
    )
    if customer is None:
        raise HTTPException(404, "Customer not found.")
    return render(request, "customers/detail.html", {"customer": customer})


@router.post("/customers/{customer_id:int}", dependencies=[Depends(verify_csrf)])
def update_customer(
    request: Request,
    customer_id: int,
    name: str = Form(""),
    notes: str = Form(""),
    marketing_consent: str | None = Form(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(404, "Customer not found.")
    if not user.can_coordinate:
        raise HTTPException(403, "Only coordinators and managers can edit customer records.")
    customer.name = svc.clean_text(name, 120)
    customer.notes = svc.clean_multiline(notes)
    consent = bool(marketing_consent)
    if consent != customer.marketing_consent:
        customer.marketing_consent = consent
        customer.consent_at = utcnow() if consent else None
    db.commit()
    flash(request, "Customer updated.")
    return RedirectResponse(f"/customers/{customer.id}", 303)

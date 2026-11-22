"""Admin: staff accounts (admin only), lead sources and product catalogue (managers)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Channel, ChannelKind, Inquiry, Product, PushSubscription, Role, User
from app.security import (
    WeakPassword,
    hash_password,
    require_admin,
    require_manager,
    verify_csrf,
)
from app.services.inquiries import clean_text
from app.templating import flash, render

router = APIRouter(prefix="/admin")


# ---------------------------------------------------------------- users
@router.get("/users")
def users(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    rows = db.scalars(select(User).order_by(User.is_active.desc(), User.full_name)).all()
    devices = dict(
        db.execute(
            select(PushSubscription.user_id, func.count()).group_by(PushSubscription.user_id)
        ).all()
    )
    return render(request, "admin/users.html", {"rows": rows, "devices": devices})


@router.get("/users/new")
def new_user_form(request: Request, user: User = Depends(require_admin)):
    return render(request, "admin/user_form.html", {"target": None, "values": {}, "error": None})


def _user_values(full_name, email, role, is_active) -> dict:
    return {"full_name": full_name, "email": email, "role": role, "is_active": is_active}


@router.post("/users/new", dependencies=[Depends(verify_csrf)])
def create_user(
    request: Request,
    full_name: str = Form(...),
    email: str = Form(...),
    role: str = Form(...),
    password: str = Form(...),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    values = _user_values(full_name, email, role, True)
    try:
        new = User(
            full_name=clean_text(full_name, 120) or "",
            email=email.strip().lower(),
            role=Role(role),
            password_hash=hash_password(password),
        )
        if not new.full_name or "@" not in new.email:
            raise ValueError("Name and a valid email are required.")
        db.add(new)
        db.commit()
    except (ValueError, WeakPassword) as exc:
        db.rollback()
        return render(
            request,
            "admin/user_form.html",
            {"target": None, "values": values, "error": str(exc)},
            status_code=422,
        )
    except IntegrityError:
        db.rollback()
        return render(
            request,
            "admin/user_form.html",
            {"target": None, "values": values, "error": "That email is already used."},
            status_code=409,
        )
    flash(request, f"Account created for {new.full_name}.")
    return RedirectResponse("/admin/users", 303)


@router.get("/users/{user_id:int}")
def edit_user_form(
    request: Request,
    user_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    target = db.get(User, user_id) or _not_found()
    values = _user_values(target.full_name, target.email, target.role.value, target.is_active)
    return render(
        request, "admin/user_form.html", {"target": target, "values": values, "error": None}
    )


@router.post("/users/{user_id:int}", dependencies=[Depends(verify_csrf)])
def update_user(
    request: Request,
    user_id: int,
    full_name: str = Form(...),
    email: str = Form(...),
    role: str = Form(...),
    is_active: str | None = Form(None),
    new_password: str = Form(""),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    target = db.get(User, user_id) or _not_found()
    values = _user_values(full_name, email, role, bool(is_active))
    try:
        new_role = Role(role)
        if target.id == user.id and (new_role != Role.ADMIN or not is_active):
            raise ValueError("You cannot remove your own admin access or deactivate yourself.")
        target.full_name = clean_text(full_name, 120) or target.full_name
        target.email = email.strip().lower()
        target.role = new_role
        target.is_active = bool(is_active)
        if new_password:
            target.password_hash = hash_password(new_password)
        db.commit()
    except (ValueError, WeakPassword) as exc:
        db.rollback()
        return render(
            request,
            "admin/user_form.html",
            {"target": target, "values": values, "error": str(exc)},
            status_code=422,
        )
    except IntegrityError:
        db.rollback()
        return render(
            request,
            "admin/user_form.html",
            {"target": target, "values": values, "error": "That email is already used."},
            status_code=409,
        )
    flash(request, f"Saved {target.full_name}.")
    return RedirectResponse("/admin/users", 303)


# ---------------------------------------------------------------- channels (lead sources)
@router.get("/channels")
def channels(
    request: Request, user: User = Depends(require_manager), db: Session = Depends(get_db)
):
    counts = dict(
        db.execute(select(Inquiry.channel_id, func.count()).group_by(Inquiry.channel_id)).all()
    )
    rows = db.scalars(select(Channel).order_by(Channel.sort_order, Channel.name)).all()
    return render(request, "admin/channels.html", {"rows": rows, "counts": counts})


@router.post("/channels", dependencies=[Depends(verify_csrf)])
def save_channel(
    request: Request,
    channel_id: str = Form(""),
    name: str = Form(...),
    kind: str = Form(...),
    phone_number: str = Form(""),
    branch: str = Form(""),
    sort_order: int = Form(100),
    is_active: str | None = Form(None),
    user: User = Depends(require_manager),
    db: Session = Depends(get_db),
):
    try:
        kind_enum = ChannelKind(kind)
    except ValueError:
        raise HTTPException(422, "Invalid channel type.") from None
    clean_name = clean_text(name, 80)
    if not clean_name:
        raise HTTPException(422, "Name is required.")
    if channel_id:
        channel = db.get(Channel, int(channel_id)) or _not_found()
    else:
        channel = Channel()
        db.add(channel)
        is_active = "on"
    channel.name = clean_name
    channel.kind = kind_enum
    channel.phone_number = clean_text(phone_number, 32)
    channel.branch = clean_text(branch, 80)
    channel.sort_order = sort_order
    channel.is_active = bool(is_active)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        flash(request, f"A source called '{clean_name}' already exists.", "error")
        return RedirectResponse("/admin/channels", 303)
    flash(request, f"Saved source '{channel.name}'.")
    return RedirectResponse("/admin/channels", 303)


# ---------------------------------------------------------------- products
@router.get("/products")
def products(
    request: Request, user: User = Depends(require_manager), db: Session = Depends(get_db)
):
    rows = db.scalars(select(Product).order_by(Product.is_active.desc(), Product.name)).all()
    return render(request, "admin/products.html", {"rows": rows})


@router.post("/products", dependencies=[Depends(verify_csrf)])
def add_products(
    request: Request,
    names: str = Form(...),
    brand: str = Form(""),
    user: User = Depends(require_manager),
    db: Session = Depends(get_db),
):
    """Add one product per line (e.g. 'iPhone 16 Pro 256GB')."""
    existing = {n.lower() for n in db.scalars(select(Product.name))}
    added = 0
    for line in names.splitlines():
        name = clean_text(line, 120)
        if name and name.lower() not in existing:
            db.add(Product(name=name, brand=clean_text(brand, 60)))
            existing.add(name.lower())
            added += 1
    db.commit()
    flash(request, f"Added {added} product(s).")
    return RedirectResponse("/admin/products", 303)


@router.post("/products/{product_id:int}/toggle", dependencies=[Depends(verify_csrf)])
def toggle_product(
    request: Request,
    product_id: int,
    user: User = Depends(require_manager),
    db: Session = Depends(get_db),
):
    product = db.get(Product, product_id) or _not_found()
    product.is_active = not product.is_active
    db.commit()
    flash(request, f"{product.name} {'activated' if product.is_active else 'hidden'}.")
    return RedirectResponse("/admin/products", 303)


def _not_found():
    raise HTTPException(404, "Not found.")

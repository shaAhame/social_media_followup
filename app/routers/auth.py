"""Login, logout and password change."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db, utcnow
from app.models import User
from app.security import (
    SESSION_USER_KEY,
    WeakPassword,
    get_current_user,
    hash_password,
    login_session,
    login_throttle,
    logout_session,
    verify_csrf,
    verify_password,
)
from app.templating import flash, render

router = APIRouter()


def _safe_next(url: str | None) -> str:
    if url and url.startswith("/") and not url.startswith("//") and "\\" not in url:
        return url
    return "/"


@router.get("/login")
def login_form(request: Request, next: str = "/"):
    if request.session.get(SESSION_USER_KEY):
        return RedirectResponse(_safe_next(next), status_code=303)
    return render(request, "login.html", {"next": _safe_next(next)})


@router.post("/login", dependencies=[Depends(verify_csrf)])
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
    db: Session = Depends(get_db),
):
    email = email.strip().lower()
    client = request.client.host if request.client else "unknown"
    key = f"{client}|{email}"
    if login_throttle.is_blocked(key):
        return render(
            request,
            "login.html",
            {"next": next, "email": email, "error": "Too many attempts. Try again in 15 minutes."},
            status_code=429,
        )
    user = db.scalar(select(User).where(func.lower(User.email) == email))
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        login_throttle.record_failure(key)
        return render(
            request,
            "login.html",
            {"next": next, "email": email, "error": "Wrong email or password."},
            status_code=400,
        )
    login_throttle.reset(key)
    user.last_login_at = utcnow()
    db.commit()
    login_session(request, user)
    return RedirectResponse(_safe_next(next), status_code=303)


@router.post("/logout", dependencies=[Depends(verify_csrf)])
def logout(request: Request):
    logout_session(request)
    return RedirectResponse("/login", status_code=303)


@router.get("/account/password")
def password_form(request: Request, user: User = Depends(get_current_user)):
    return render(request, "password.html")


@router.post("/account/password", dependencies=[Depends(verify_csrf)])
def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    error = None
    if not verify_password(current_password, user.password_hash):
        error = "Current password is wrong."
    elif new_password != confirm_password:
        error = "The new passwords do not match."
    else:
        try:
            user.password_hash = hash_password(new_password)
        except WeakPassword as exc:
            error = str(exc)
    if error:
        return render(request, "password.html", {"error": error}, status_code=400)
    db.commit()
    flash(request, "Password changed.")
    return RedirectResponse("/", status_code=303)

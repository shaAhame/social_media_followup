"""Authentication, authorisation and CSRF protection."""

from __future__ import annotations

import secrets
import time
from collections import defaultdict, deque
from collections.abc import Callable

import bcrypt
from fastapi import Depends, Form, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Inquiry, Role, User

MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_BYTES = 72  # bcrypt limit


# ---------------------------------------------------------------- passwords
class WeakPassword(ValueError):
    pass


def validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPassword(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if len(password.encode()) > MAX_PASSWORD_BYTES:
        raise WeakPassword("Password is too long (max 72 bytes).")


def hash_password(password: str) -> str:
    validate_password(password)
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode()[:MAX_PASSWORD_BYTES], password_hash.encode())
    except ValueError:
        return False


# ---------------------------------------------------------------- sessions
class LoginRequired(Exception):
    """Raised by dependencies; turned into a redirect to /login by an exception handler."""

    def __init__(self, next_url: str = "/") -> None:
        self.next_url = next_url


SESSION_USER_KEY = "uid"
SESSION_CSRF_KEY = "csrf"


def login_session(request: Request, user: User) -> None:
    request.session.clear()  # prevent session fixation
    request.session[SESSION_USER_KEY] = user.id
    request.session[SESSION_CSRF_KEY] = secrets.token_urlsafe(32)


def logout_session(request: Request) -> None:
    request.session.clear()


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user_id = request.session.get(SESSION_USER_KEY)
    user = db.get(User, user_id) if user_id else None
    if user is None or not user.is_active:
        request.session.pop(SESSION_USER_KEY, None)
        target = request.url.path
        if request.url.query:
            target += "?" + request.url.query
        raise LoginRequired(target)
    request.state.user = user
    return user


def require_roles(*roles: Role) -> Callable[..., User]:
    allowed = set(roles)

    def dependency(user: User = Depends(get_current_user)) -> User:
        if user.role not in allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "You do not have access to this page.")
        return user

    return dependency


require_manager = require_roles(Role.ADMIN, Role.MANAGER)
require_coordinator = require_roles(Role.ADMIN, Role.MANAGER, Role.COORDINATOR)
require_admin = require_roles(Role.ADMIN)


def can_edit_inquiry(user: User, inquiry: Inquiry) -> bool:
    """Coordinators and managers edit everything; sales edit their own or unassigned leads."""
    if user.can_coordinate:
        return True
    return inquiry.assigned_to_id in (None, user.id)


# ---------------------------------------------------------------- CSRF
def csrf_token(request: Request) -> str:
    token = request.session.get(SESSION_CSRF_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[SESSION_CSRF_KEY] = token
    return token


def verify_csrf(request: Request, csrf_token_field: str = Form("", alias="csrf_token")) -> None:
    expected = request.session.get(SESSION_CSRF_KEY)
    if not expected or not secrets.compare_digest(expected, csrf_token_field or ""):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Your form expired or was submitted from another site. Please reload and try again.",
        )


def verify_csrf_header(request: Request) -> None:
    """CSRF check for JSON requests made by the app's own JavaScript."""
    expected = request.session.get(SESSION_CSRF_KEY)
    sent = request.headers.get("x-csrf-token", "")
    if not expected or not secrets.compare_digest(expected, sent):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid or missing CSRF token.")


# ---------------------------------------------------------------- login throttling
class LoginThrottle:
    """Simple in-memory limiter: N failed attempts per key per window.

    Good enough for a single-instance deployment; swap for Redis if you scale out.
    """

    def __init__(self, max_attempts: int = 8, window_seconds: int = 900) -> None:
        self.max_attempts = max_attempts
        self.window = window_seconds
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, key: str, now: float) -> deque[float]:
        q = self._failures[key]
        while q and now - q[0] > self.window:
            q.popleft()
        return q

    def is_blocked(self, key: str) -> bool:
        return len(self._prune(key, time.monotonic())) >= self.max_attempts

    def record_failure(self, key: str) -> None:
        now = time.monotonic()
        self._prune(key, now).append(now)

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)


login_throttle = LoginThrottle()

"""Test fixtures.

Runs on SQLite by default. Set TEST_DATABASE_URL to run the same suite on PostgreSQL:
    TEST_DATABASE_URL=postgresql://postgres@localhost:5432/leads_test pytest
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

_tmp = Path(tempfile.mkdtemp())
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{_tmp / 'test.db'}"
os.environ["SECRET_KEY"] = "test-secret-key-that-is-long-enough-123456"
os.environ["API_KEY"] = "test-api-key"
os.environ["ENVIRONMENT"] = "development"
os.environ["SEED_DEFAULT_CHANNELS"] = "true"
os.environ["RUN_SCHEDULER"] = "false"
for key in ("INITIAL_ADMIN_EMAIL", "INITIAL_ADMIN_PASSWORD"):
    os.environ.pop(key, None)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import Base, SessionLocal, init_engine  # noqa: E402
from app.models import Channel, Role, User  # noqa: E402
from app.security import hash_password, login_throttle  # noqa: E402
from app.services.bootstrap import seed_default_channels  # noqa: E402

PASSWORD = "correct-horse-1"
engine = init_engine(os.environ["DATABASE_URL"])


@pytest.fixture(autouse=True)
def fresh_database():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        seed_default_channels(db)
        db.commit()
    login_throttle._failures.clear()
    yield


@pytest.fixture
def settings():
    return get_settings()


@pytest.fixture
def db():
    with SessionLocal() as session:
        yield session


@pytest.fixture
def make_user(db):
    def _make(role: Role = Role.SALES, name: str | None = None, email: str | None = None) -> User:
        name = name or f"{role.value.title()} User"
        email = email or f"{name.lower().replace(' ', '.')}@example.com"
        user = User(full_name=name, email=email, role=role, password_hash=hash_password(PASSWORD))
        db.add(user)
        db.commit()
        return user

    return _make


@pytest.fixture
def channels(db) -> list[Channel]:
    return list(db.scalars(select(Channel).order_by(Channel.sort_order)))


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


def csrf_from(client: TestClient, path: str = "/login") -> str:
    response = client.get(path)
    match = CSRF_RE.search(response.text)
    assert match, f"no CSRF token on {path} ({response.status_code})"
    return match.group(1)


def login(client: TestClient, user: User) -> TestClient:
    token = csrf_from(client, "/login")
    response = client.post(
        "/login",
        data={"email": user.email, "password": PASSWORD, "csrf_token": token, "next": "/"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return client


def post_form(client: TestClient, path: str, data: dict, token_page: str = "/", **kwargs):
    token = csrf_from(client, token_page)
    return client.post(path, data={**data, "csrf_token": token}, follow_redirects=False, **kwargs)

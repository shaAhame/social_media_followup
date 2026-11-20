"""Database engine, session factory and shared column types."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

from sqlalchemy import DateTime, Engine, MetaData, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import TypeDecorator

from app.config import get_settings

# Deterministic constraint names keep Alembic migrations stable across databases.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UTCDateTime(TypeDecorator):
    """Timezone-aware datetime that is always stored and returned in UTC.

    PostgreSQL keeps the offset natively (timestamptz); SQLite does not, so values
    read back without tzinfo are re-labelled as UTC. Naive datetimes are rejected on
    write to prevent silent local/UTC mix-ups.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):  # noqa: ARG002
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Naive datetime passed to UTCDateTime column; use utcnow()")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect):  # noqa: ARG002
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def utcnow() -> datetime:
    return datetime.now(UTC)


_engine: Engine | None = None
SessionLocal = sessionmaker(autoflush=False, expire_on_commit=False)


def init_engine(url: str | None = None) -> Engine:
    """(Re)create the global engine. Called lazily, and explicitly by tests."""
    global _engine
    url = url or get_settings().database_url
    kwargs: dict = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        # Small pool: the app runs as a single low-memory container.
        kwargs.update(pool_size=5, max_overflow=5, pool_recycle=1800)
    if _engine is not None:
        _engine.dispose()
    _engine = create_engine(url, **kwargs)
    SessionLocal.configure(bind=_engine)
    return _engine


def get_engine() -> Engine:
    return _engine or init_engine()


def get_db() -> Iterator[Session]:
    get_engine()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

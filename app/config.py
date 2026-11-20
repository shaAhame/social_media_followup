"""Application settings, loaded from environment variables (or a local .env file)."""

from __future__ import annotations

from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

INSECURE_DEFAULT_SECRET = "dev-only-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Branding
    app_name: str = "iDealz Lead Tracker"
    company_name: str = "iDealz Lanka"

    # Runtime
    environment: str = "development"  # "development" | "production"
    database_url: str = "sqlite:///./leads.db"
    secret_key: str = INSECURE_DEFAULT_SECRET
    session_max_age_hours: int = 12
    session_https_only: bool = False

    # Locale
    timezone: str = "Asia/Colombo"
    default_phone_region: str = "LK"

    # Follow-up engine
    followup_max_attempts: int = 4
    business_day_start_hour: int = 10
    business_day_end_hour: int = 20
    whatsapp_followup_template: str = (
        "Hi {name}, this is {agent} from {company}. Following up on the {product} "
        "you asked about. Can I help you with the price, stock or installment options?"
    )

    # Integrations: key required by POST /api/v1/inquiries (leave empty to disable the API)
    api_key: str | None = None

    # First-run bootstrap
    initial_admin_email: str | None = None
    initial_admin_password: str | None = None
    initial_admin_name: str = "Administrator"
    seed_default_channels: bool = True

    @field_validator("database_url")
    @classmethod
    def _use_psycopg_driver(cls, value: str) -> str:
        # Railway / Heroku style URLs -> SQLAlchemy + psycopg v3 driver
        if value.startswith("postgres://"):
            return "postgresql+psycopg://" + value[len("postgres://") :]
        if value.startswith("postgresql://"):
            return "postgresql+psycopg://" + value[len("postgresql://") :]
        return value

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def validate_for_runtime(self) -> None:
        """Fail fast on unsafe production configuration."""
        if self.is_production:
            if self.secret_key == INSECURE_DEFAULT_SECRET or len(self.secret_key) < 32:
                raise RuntimeError(
                    "SECRET_KEY must be a random value of at least 32 characters in production. "
                    "Generate one: python -c 'import secrets;print(secrets.token_urlsafe(48))'"
                )
            if self.database_url.startswith("sqlite"):
                raise RuntimeError("Use PostgreSQL (DATABASE_URL) in production, not SQLite.")


@lru_cache
def get_settings() -> Settings:
    return Settings()

from pathlib import Path

from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parents[1]


def test_migrations_upgrade_downgrade_and_match_models(tmp_path):
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{tmp_path / 'migrate.db'}")
    command.upgrade(cfg, "head")
    command.check(cfg)  # raises if models and migrations have drifted apart
    command.downgrade(cfg, "base")

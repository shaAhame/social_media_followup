"""planned purchase date

Adds Inquiry.planned_date for the "Pick a date" time period (timeframe = 'on_date').

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-22
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("inquiries", sa.Column("planned_date", sa.Date(), nullable=True))


def downgrade() -> None:
    # The previous version doesn't know 'on_date'; treat those leads as "This week".
    op.execute("UPDATE inquiries SET timeframe = 'this_week' WHERE timeframe = 'on_date'")
    op.drop_column("inquiries", "planned_date")

"""allow project lead email to be null

Revision ID: a9f3c2e7b614
Revises: d6e1f8a2c4b7
Create Date: 2026-08-31 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a9f3c2e7b614"
down_revision: Union[str, Sequence[str], None] = "d6e1f8a2c4b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "project",
        "project_lead_email",
        existing_type=sa.String(),
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "project",
        "project_lead_email",
        existing_type=sa.String(),
        nullable=False,
    )

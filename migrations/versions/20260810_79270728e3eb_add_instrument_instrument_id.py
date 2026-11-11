"""add instrument.instrument_id

A validated, human-readable handle for instruments, separate from
instrument_name (free-text display) and unique_id (MFID, used for the
instrument's operator access group). See docs/permissions.md,
"Instruments and service accounts".

Nullable on existing rows until the backfill runs:
    DB_CONF_FILE=.env python migrations/data/<date>_backfill_instrument_id.py --commit

Revision ID: 79270728e3eb
Revises: 4ae6482731ed
Create Date: 2026-08-10 17:51:13.313426

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '79270728e3eb'
down_revision: Union[str, Sequence[str], None] = '4ae6482731ed'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("instrument", sa.Column("instrument_id", sa.String(), nullable=True))
    # Partial: skips NULLs so existing rows don't collide before the backfill
    # runs, matching add_project_unique_id's convention for the same case.
    op.create_index(
        "ix_instrument_instrument_id", "instrument", ["instrument_id"],
        unique=True, postgresql_where=sa.text("instrument_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_instrument_instrument_id", table_name="instrument")
    op.drop_column("instrument", "instrument_id")

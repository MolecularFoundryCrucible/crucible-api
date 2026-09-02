"""enforce access group type

Revision ID: c4a7e9d2b615
Revises: 7b2d4e6f8a10, 91f3c8a4d2e7
Create Date: 2026-08-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c4a7e9d2b615"
down_revision: Union[str, Sequence[str], None] = (
    "7b2d4e6f8a10",
    "91f3c8a4d2e7",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM accessgroup WHERE group_type IS NULL) THEN
                RAISE EXCEPTION 'accessgroup.group_type contains NULL values';
            END IF;
        END
        $$
    """)
    op.alter_column(
        "accessgroup",
        "group_type",
        existing_type=sa.Enum(
            "identity",
            "project",
            "instrument",
            "custom",
            "system",
            name="access_group_type",
        ),
        nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "accessgroup",
        "group_type",
        existing_type=sa.Enum(
            "identity",
            "project",
            "instrument",
            "custom",
            "system",
            name="access_group_type",
        ),
        nullable=True,
    )

"""add deletion audit project mfid

Revision ID: 4a7d2c9e1f60
Revises: 9e4c2a7b6d31
Create Date: 2026-09-23 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "4a7d2c9e1f60"
down_revision: Union[str, Sequence[str], None] = "9e4c2a7b6d31"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "deletionauditlog",
        sa.Column("project_mfid", sa.String(), nullable=True),
    )
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_deletionauditlog_project_mfid",
            "deletionauditlog",
            ["project_mfid"],
            unique=False,
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_deletionauditlog_project_mfid",
            table_name="deletionauditlog",
            postgresql_concurrently=True,
        )
    op.drop_column("deletionauditlog", "project_mfid")

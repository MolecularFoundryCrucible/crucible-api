"""add nullable access group type

Revision ID: 91f3c8a4d2e7
Revises: e5d68f53b42c
Create Date: 2026-08-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "91f3c8a4d2e7"
down_revision: Union[str, Sequence[str], None] = "e5d68f53b42c"
branch_labels: Union[str, Sequence[str], None] = ("access_group_type",)
depends_on: Union[str, Sequence[str], None] = None


VALUES = ("identity", "project", "instrument", "custom", "system")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        enum_type = postgresql.ENUM(*VALUES, name="access_group_type")
        enum_type.create(bind, checkfirst=True)
        column_type = postgresql.ENUM(
            *VALUES,
            name="access_group_type",
            create_type=False,
        )
    else:
        column_type = sa.String(length=16)

    op.add_column(
        "accessgroup",
        sa.Column("group_type", column_type, nullable=True),
    )
    op.create_index(
        "ix_accessgroup_group_type",
        "accessgroup",
        ["group_type"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_accessgroup_group_type", table_name="accessgroup")
    op.drop_column("accessgroup", "group_type")
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        postgresql.ENUM(name="access_group_type").drop(bind, checkfirst=True)

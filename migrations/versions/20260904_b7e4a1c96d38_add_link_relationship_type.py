"""add nullable relationship_type to datasetlink and samplelink

Revision ID: b7e4a1c96d38
Revises: f3a8c1d7e204
Create Date: 2026-09-04 00:00:00.000000

Existing rows are left NULL: the kind of relationship was never recorded, so
there is nothing to backfill from. NULL means "unspecified".

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "b7e4a1c96d38"
down_revision: Union[str, Sequence[str], None] = "f3a8c1d7e204"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ENUM_NAME = "link_relationship_type"
VALUES = ("is_derived_from", "is_part_of")
TABLES = ("datasetlink", "samplelink")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        enum_type = postgresql.ENUM(*VALUES, name=ENUM_NAME)
        enum_type.create(bind, checkfirst=True)
        column_type = postgresql.ENUM(*VALUES, name=ENUM_NAME, create_type=False)
    else:
        column_type = sa.String(length=32)

    for table in TABLES:
        op.add_column(
            table,
            sa.Column("relationship_type", column_type, nullable=True),
        )
        op.create_index(
            f"ix_{table}_relationship_type",
            table,
            ["relationship_type"],
            unique=False,
        )


def downgrade() -> None:
    for table in TABLES:
        op.drop_index(f"ix_{table}_relationship_type", table_name=table)
        op.drop_column(table, "relationship_type")

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        postgresql.ENUM(name=ENUM_NAME).drop(bind, checkfirst=True)

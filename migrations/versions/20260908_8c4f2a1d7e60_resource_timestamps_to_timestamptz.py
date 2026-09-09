"""convert resource timestamps to timestamptz

Revision ID: 8c4f2a1d7e60
Revises: 6e2f9c4a8b17
Create Date: 2026-09-08 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "8c4f2a1d7e60"
down_revision: Union[str, Sequence[str], None] = "6e2f9c4a8b17"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text(r"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM idtype
                WHERE
                    (creation_time IS NOT NULL AND creation_time !~ '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$')
                    OR
                    (modification_time IS NOT NULL AND modification_time !~ '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$')
            ) THEN
                RAISE EXCEPTION 'idtype contains a resource timestamp without an explicit timezone';
            END IF;
        END
        $$
    """))

    op.alter_column(
        "idtype",
        "creation_time",
        existing_type=sa.String(),
        type_=sa.DateTime(timezone=True),
        existing_nullable=True,
        postgresql_using="creation_time::timestamptz",
    )
    op.alter_column(
        "idtype",
        "modification_time",
        existing_type=sa.String(),
        type_=sa.DateTime(timezone=True),
        existing_nullable=True,
        postgresql_using="modification_time::timestamptz",
    )


def downgrade() -> None:
    op.alter_column(
        "idtype",
        "modification_time",
        existing_type=sa.DateTime(timezone=True),
        type_=sa.String(),
        existing_nullable=True,
        postgresql_using=(
            "to_char(modification_time AT TIME ZONE 'UTC', "
            "'YYYY-MM-DD\"T\"HH24:MI:SS.US') || '+00:00'"
        ),
    )
    op.alter_column(
        "idtype",
        "creation_time",
        existing_type=sa.DateTime(timezone=True),
        type_=sa.String(),
        existing_nullable=True,
        postgresql_using=(
            "to_char(creation_time AT TIME ZONE 'UTC', "
            "'YYYY-MM-DD\"T\"HH24:MI:SS.US') || '+00:00'"
        ),
    )

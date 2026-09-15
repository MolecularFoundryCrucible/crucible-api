"""convert resource timestamps to timestamptz

Revision ID: 8c4f2a1d7e60
Revises: b7e4a1c96d38
Create Date: 2026-09-08 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "8c4f2a1d7e60"
down_revision: Union[str, Sequence[str], None] = "b7e4a1c96d38"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ALLOWED_TIMESTAMP_PATTERN = (
    r"^("
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}(:\d{2})?)"
    r"|\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d{1,6})?"
    r"|\d{4}-\d{2}-\d{2}"
    r"|\d{8}"
    r"|\d{4}-\d{2}-\d{2}_\d{6}"
    r"|\d{8}_(am|pm)"
    r")$"
)


def _timestamp_conversion(column_name: str) -> str:
    return (
        "CASE "
        f"WHEN {column_name} IS NULL OR {column_name} IN ('', 'NaN') THEN NULL "
        f"WHEN {column_name} ~ '^\\d{{4}}-\\d{{2}}-\\d{{2}}$' "
        f"THEN {column_name}::date::timestamp AT TIME ZONE 'America/Los_Angeles' "
        f"WHEN {column_name} ~ '^\\d{{8}}$' "
        f"THEN to_date({column_name}, 'YYYYMMDD')::timestamp AT TIME ZONE 'America/Los_Angeles' "
        f"WHEN {column_name} ~ '^\\d{{4}}-\\d{{2}}-\\d{{2}}_\\d{{6}}$' "
        f"THEN (to_date(left({column_name}, 10), 'YYYY-MM-DD') + "
        f"to_timestamp(right({column_name}, 6), 'HH24MISS')::time) AT TIME ZONE 'America/Los_Angeles' "
        f"WHEN {column_name} ~ '^\\d{{8}}_(am|pm)$' "
        f"THEN (to_date(left({column_name}, 8), 'YYYYMMDD')::timestamp + "
        f"CASE WHEN right({column_name}, 2) = 'pm' THEN interval '12 hours' ELSE interval '0 hours' END) "
        "AT TIME ZONE 'America/Los_Angeles' "
        f"WHEN {column_name} ~ '^\\d{{4}}-\\d{{2}}-\\d{{2}}[T ]\\d{{2}}:\\d{{2}}:\\d{{2}}(\\.\\d{{1,6}})?$' "
        f"THEN {column_name}::timestamp AT TIME ZONE 'America/Los_Angeles' "
        f"ELSE {column_name}::timestamptz "
        "END"
    )


def upgrade() -> None:
    op.execute(sa.text(rf"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM idtype
                WHERE
                    (
                        creation_time IS NOT NULL
                        AND creation_time NOT IN ('', 'NaN')
                        AND creation_time !~ '{ALLOWED_TIMESTAMP_PATTERN}'
                    )
                    OR
                    (
                        modification_time IS NOT NULL
                        AND modification_time NOT IN ('', 'NaN')
                        AND modification_time !~ '{ALLOWED_TIMESTAMP_PATTERN}'
                    )
            ) THEN
                RAISE EXCEPTION 'idtype contains an invalid non-null resource timestamp';
            END IF;
        END
        $$
    """))

    op.execute(sa.text("""
        UPDATE idtype
        SET
            creation_time = CASE WHEN creation_time IN ('', 'NaN') THEN NULL ELSE creation_time END,
            modification_time = CASE WHEN modification_time IN ('', 'NaN') THEN NULL ELSE modification_time END
        WHERE creation_time IN ('', 'NaN') OR modification_time IN ('', 'NaN')
    """))

    op.execute(sa.text("""
        UPDATE idtype
        SET modification_time = creation_time
        WHERE modification_time IS NULL AND creation_time IS NOT NULL
    """))

    op.alter_column(
        "idtype",
        "creation_time",
        existing_type=sa.String(),
        type_=sa.DateTime(timezone=True),
        existing_nullable=True,
        nullable=True,
        server_default=sa.text("now()"),
        postgresql_using=_timestamp_conversion("creation_time"),
    )
    op.alter_column(
        "idtype",
        "modification_time",
        existing_type=sa.String(),
        type_=sa.DateTime(timezone=True),
        existing_nullable=True,
        nullable=True,
        server_default=sa.text("now()"),
        postgresql_using=_timestamp_conversion("modification_time"),
    )

def downgrade() -> None:
    op.alter_column(
        "idtype",
        "modification_time",
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=True,
        server_default=None,
    )
    op.alter_column(
        "idtype",
        "modification_time",
        existing_type=sa.DateTime(timezone=True),
        type_=sa.String(),
        existing_nullable=True,
        nullable=True,
        postgresql_using=(
            "to_char(modification_time AT TIME ZONE 'UTC', "
            "'YYYY-MM-DD\"T\"HH24:MI:SS.US') || '+00:00'"
        ),
    )
    op.alter_column(
        "idtype",
        "creation_time",
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=True,
        server_default=None,
    )
    op.alter_column(
        "idtype",
        "creation_time",
        existing_type=sa.DateTime(timezone=True),
        type_=sa.String(),
        existing_nullable=True,
        nullable=True,
        postgresql_using=(
            "to_char(creation_time AT TIME ZONE 'UTC', "
            "'YYYY-MM-DD\"T\"HH24:MI:SS.US') || '+00:00'"
        ),
    )

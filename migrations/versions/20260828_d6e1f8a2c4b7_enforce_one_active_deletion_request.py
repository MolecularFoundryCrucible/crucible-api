"""enforce one active deletion request per resource

Revision ID: d6e1f8a2c4b7
Revises: c4a7e9d2b615
Create Date: 2026-08-28 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d6e1f8a2c4b7"
down_revision: Union[str, Sequence[str], None] = "c4a7e9d2b615"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM deletionrequest
                WHERE status IN ('pending', 'approved')
                GROUP BY resource_id
                HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION 'deletionrequest contains resources with multiple active requests';
            END IF;
        END
        $$
    """)
    op.create_index(
        "one_active_deletion_request_per_resource",
        "deletionrequest",
        ["resource_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'approved')"),
    )


def downgrade() -> None:
    op.drop_index(
        "one_active_deletion_request_per_resource",
        table_name="deletionrequest",
    )

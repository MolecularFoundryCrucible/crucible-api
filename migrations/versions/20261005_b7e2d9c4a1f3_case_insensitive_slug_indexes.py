"""enforce case-insensitive project and instrument slugs

Revision ID: b7e2d9c4a1f3
Revises: e92a6b1c730f
Create Date: 2026-10-05 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b7e2d9c4a1f3"
down_revision: Union[str, Sequence[str], None] = "e92a6b1c730f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEXES = (
    ("ux_project_project_id_lower", "project", "project_id"),
    ("ux_instrument_instrument_id_lower", "instrument", "instrument_id"),
)


def upgrade() -> None:
    bind = op.get_bind()
    for _, table, column in INDEXES:
        duplicate = bind.execute(sa.text(f"""
            SELECT lower({column}) AS folded
            FROM {table}
            WHERE {column} IS NOT NULL
            GROUP BY lower({column})
            HAVING count(*) > 1
            LIMIT 1
        """)).first()
        if duplicate is not None:
            raise RuntimeError(
                f"{table}.{column} contains values differing only by case: {duplicate.folded!r}. "
                "Rename one of each pair, then run the upgrade again."
            )

    with op.get_context().autocommit_block():
        for name, table, column in INDEXES:
            op.drop_index(name, table_name=table, if_exists=True, postgresql_concurrently=True)
            op.create_index(
                name,
                table,
                [sa.text(f"lower({column})")],
                unique=True,
                postgresql_concurrently=True,
            )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name, table, _ in reversed(INDEXES):
            op.drop_index(name, table_name=table, if_exists=True, postgresql_concurrently=True)

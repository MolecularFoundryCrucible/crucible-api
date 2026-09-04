"""index thumbnail dataset id

Revision ID: 6e2f9c4a8b17
Revises: f3a8c1d7e204
Create Date: 2026-09-04 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = "6e2f9c4a8b17"
down_revision: Union[str, Sequence[str], None] = "f3a8c1d7e204"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX_NAME = "ix_thumbnail_dataset_id"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            INDEX_NAME,
            "thumbnail",
            ["dataset_id"],
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            INDEX_NAME,
            table_name="thumbnail",
            postgresql_concurrently=True,
        )

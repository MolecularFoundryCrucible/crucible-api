"""index reverse graph links

Revision ID: 5c8e1a4d7b20
Revises: 7a1c4e8d2f90
Create Date: 2026-09-17 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = "5c8e1a4d7b20"
down_revision: Union[str, Sequence[str], None] = "7a1c4e8d2f90"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEXES = (
    ("ix_samplelink_parent_sample_id", "samplelink", "parent_sample_id"),
    ("ix_datasetsamplelink_dataset_id", "datasetsamplelink", "dataset_id"),
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for index_name, table_name, column_name in INDEXES:
            op.create_index(
                index_name,
                table_name,
                [column_name],
                postgresql_concurrently=True,
            )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for index_name, table_name, _ in reversed(INDEXES):
            op.drop_index(
                index_name,
                table_name=table_name,
                postgresql_concurrently=True,
            )

"""accessgroupjoinrequest.group_name to accessgroup_id

The migration resolves every existing group_name through accessgroup before making accessgroup_id non-null and removing the legacy column. Routes keep accepting project references on the API surface while the database stores the stable group key.

Revision ID: 454c96264cfb
Revises: bb780697b63b
Create Date: 2026-08-13 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '454c96264cfb'
down_revision: Union[str, Sequence[str], None] = 'bb780697b63b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("accessgroupjoinrequest", sa.Column("accessgroup_id", sa.Integer(), nullable=True))
    op.execute("""
        UPDATE accessgroupjoinrequest request
        SET accessgroup_id = access_group.id
        FROM accessgroup access_group
        WHERE access_group.group_name = request.group_name
    """)
    op.alter_column("accessgroupjoinrequest", "accessgroup_id", nullable=False)
    op.create_index("ix_agjr_accessgroup_id", "accessgroupjoinrequest", ["accessgroup_id"])
    op.create_foreign_key(
        "accessgroupjoinrequest_accessgroup_id_fkey", "accessgroupjoinrequest", "accessgroup",
        ["accessgroup_id"], ["id"],
    )
    op.drop_index("ix_agjr_group_name", table_name="accessgroupjoinrequest")
    op.drop_constraint("accessgroupjoinrequest_group_name_fkey", "accessgroupjoinrequest", type_="foreignkey")
    op.drop_column("accessgroupjoinrequest", "group_name")


def downgrade() -> None:
    op.add_column("accessgroupjoinrequest", sa.Column("group_name", sa.String(), nullable=True))
    op.execute("""
        UPDATE accessgroupjoinrequest request
        SET group_name = access_group.group_name
        FROM accessgroup access_group
        WHERE access_group.id = request.accessgroup_id
    """)
    op.alter_column("accessgroupjoinrequest", "group_name", nullable=False)
    op.create_index("ix_agjr_group_name", "accessgroupjoinrequest", ["group_name"])
    op.create_foreign_key(
        "accessgroupjoinrequest_group_name_fkey", "accessgroupjoinrequest", "accessgroup",
        ["group_name"], ["group_name"],
    )
    op.drop_constraint("accessgroupjoinrequest_accessgroup_id_fkey", "accessgroupjoinrequest", type_="foreignkey")
    op.drop_index("ix_agjr_accessgroup_id", table_name="accessgroupjoinrequest")
    op.drop_column("accessgroupjoinrequest", "accessgroup_id")

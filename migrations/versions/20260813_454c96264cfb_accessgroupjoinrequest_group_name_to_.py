"""accessgroupjoinrequest.group_name to accessgroup_id

group_name FK'd to accessgroup.group_name, which has meant a project's MFID
(Project.unique_id) since the C4b project-group rename — but every route in
app/api/routes/access_groups.py writes and queries this column as if it
were a project's project_id (the human label). Since the rename shipped, any
real insert here should have violated the FK (project_id no longer matches
any accessgroup.group_name row), making POST /access_groups/{group_name}/join
broken in practice. No test exercised it against a real FK-enforcing
database, so it went unnoticed. 0 rows on staging, confirmed before writing
this migration, so no backfill needed — this is a straight column swap.

Switches to accessgroup_id (int, FK -> accessgroup.id), an internal
surrogate key with no label/MFID ambiguity to get wrong. Routes keep
accepting and returning a project_id string on the API surface; the FK
target is a pure implementation detail now.

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
    op.drop_index("ix_agjr_group_name", table_name="accessgroupjoinrequest")
    op.drop_constraint("accessgroupjoinrequest_group_name_fkey", "accessgroupjoinrequest", type_="foreignkey")
    op.drop_column("accessgroupjoinrequest", "group_name")

    op.add_column("accessgroupjoinrequest", sa.Column("accessgroup_id", sa.Integer(), nullable=False))
    op.create_index("ix_agjr_accessgroup_id", "accessgroupjoinrequest", ["accessgroup_id"])
    op.create_foreign_key(
        "accessgroupjoinrequest_accessgroup_id_fkey", "accessgroupjoinrequest", "accessgroup",
        ["accessgroup_id"], ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("accessgroupjoinrequest_accessgroup_id_fkey", "accessgroupjoinrequest", type_="foreignkey")
    op.drop_index("ix_agjr_accessgroup_id", table_name="accessgroupjoinrequest")
    op.drop_column("accessgroupjoinrequest", "accessgroup_id")

    op.add_column("accessgroupjoinrequest", sa.Column("group_name", sa.String(), nullable=False))
    op.create_index("ix_agjr_group_name", "accessgroupjoinrequest", ["group_name"])
    op.create_foreign_key(
        "accessgroupjoinrequest_group_name_fkey", "accessgroupjoinrequest", "accessgroup",
        ["group_name"], ["group_name"],
    )

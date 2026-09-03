"""enforce one pending join request per group and requester

Revision ID: f3a8c1d7e204
Revises: a9f3c2e7b614
Create Date: 2026-09-02 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f3a8c1d7e204"
down_revision: Union[str, Sequence[str], None] = "a9f3c2e7b614"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX_NAME = "one_pending_join_request_per_group_requester"


def upgrade() -> None:
    duplicate = op.get_bind().execute(sa.text("""
        SELECT accessgroup_id, requester_id
        FROM accessgroupjoinrequest
        WHERE status = 'pending'
        GROUP BY accessgroup_id, requester_id
        HAVING count(*) > 1
        LIMIT 1
    """)).first()
    if duplicate is not None:
        raise RuntimeError(
            "accessgroupjoinrequest contains duplicate pending requests for "
            f"accessgroup_id={duplicate.accessgroup_id}, requester_id={duplicate.requester_id!r}"
        )

    with op.get_context().autocommit_block():
        op.drop_index(
            INDEX_NAME,
            table_name="accessgroupjoinrequest",
            if_exists=True,
            postgresql_concurrently=True,
        )
        op.create_index(
            INDEX_NAME,
            "accessgroupjoinrequest",
            ["accessgroup_id", "requester_id"],
            unique=True,
            postgresql_where=sa.text("status = 'pending'"),
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            INDEX_NAME,
            table_name="accessgroupjoinrequest",
            if_exists=True,
            postgresql_concurrently=True,
        )

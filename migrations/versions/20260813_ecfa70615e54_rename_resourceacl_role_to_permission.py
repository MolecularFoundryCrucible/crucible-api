"""rename resourceacl.role to permission

ResourceACL.role and UserAccessGroupLink.role were both named "role" despite
meaning different things -- a user's standing within a group vs. what the
group was granted on a resource -- overloading the word confusingly (see
docs/permission_system_plan.md's terminology discussion). Settled on: the
group's grant is a "permission", a user's standing stays a "role".

Pure column rename, same resource_role enum type (still shared with
useraccessgrouplink.role, which is untouched). ALTER TABLE ... RENAME COLUMN
is a catalog-only change on Postgres, not a table rewrite -- fast regardless
of table size. This is a hard cutover, not expand/contract: old code reading
.role and new code reading .permission cannot both work against the same
schema, so code and schema deploy together, back to back.

Revision ID: ecfa70615e54
Revises: 454c96264cfb
Create Date: 2026-08-13 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'ecfa70615e54'
down_revision: Union[str, Sequence[str], None] = '454c96264cfb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("resourceacl", "role", new_column_name="permission")
    op.execute("ALTER INDEX ix_resourceacl_group_mfid_role RENAME TO ix_resourceacl_group_mfid_permission")


def downgrade() -> None:
    op.execute("ALTER INDEX ix_resourceacl_group_mfid_permission RENAME TO ix_resourceacl_group_mfid_role")
    op.alter_column("resourceacl", "permission", new_column_name="role")

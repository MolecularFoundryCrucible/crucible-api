"""rename resourceacl.role to permission

Staging originally introduced ResourceACL.role and later renamed it to
permission. Production starts from a schema with neither column and introduces
permission directly. This revision preserves the staging path while becoming a
no-op when the final column and index names already exist.

Revision ID: ecfa70615e54
Revises: 454c96264cfb
Create Date: 2026-08-13 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ecfa70615e54'
down_revision: Union[str, Sequence[str], None] = '454c96264cfb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("resourceacl")}
    if "role" in columns and "permission" in columns:
        raise RuntimeError("resourceacl contains both role and permission")
    if "role" in columns:
        op.alter_column("resourceacl", "role", new_column_name="permission")
    elif "permission" not in columns:
        raise RuntimeError("resourceacl contains neither role nor permission")

    indexes = {index["name"] for index in inspector.get_indexes("resourceacl")}
    old_index = "ix_resourceacl_group_mfid_role"
    new_index = "ix_resourceacl_group_mfid_permission"
    if old_index in indexes and new_index in indexes:
        raise RuntimeError("resourceacl contains both role and permission covering indexes")
    if old_index in indexes:
        op.execute(f"ALTER INDEX {old_index} RENAME TO {new_index}")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("resourceacl")}
    if "role" in columns or "permission" not in columns:
        raise RuntimeError("resourceacl is not in the permission-column state")

    indexes = {index["name"] for index in inspector.get_indexes("resourceacl")}
    if "ix_resourceacl_group_mfid_permission" in indexes:
        op.execute("ALTER INDEX ix_resourceacl_group_mfid_permission RENAME TO ix_resourceacl_group_mfid_role")
    op.alter_column("resourceacl", "permission", new_column_name="role")

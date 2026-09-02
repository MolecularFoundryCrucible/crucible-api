"""enforce permission role invariants

This revision rejects incomplete authorization rows and prevents more than one
owner ACL on a resource. It deliberately permits resources and groups with no
owner. Apply only after old writers are stopped. Either deploy the compatible
API first or keep writes paused until that deployment completes.

Revision ID: 7b2d4e6f8a10
Revises: ecfa70615e54
Create Date: 2026-08-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "7b2d4e6f8a10"
down_revision: Union[str, Sequence[str], None] = "ecfa70615e54"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM resourceacl WHERE permission IS NULL) THEN
                RAISE EXCEPTION 'resourceacl.permission contains NULL values';
            END IF;
            IF EXISTS (SELECT 1 FROM useraccessgrouplink WHERE role IS NULL) THEN
                RAISE EXCEPTION 'useraccessgrouplink.role contains NULL values';
            END IF;
            IF EXISTS (
                SELECT 1
                FROM resourceacl
                WHERE permission = 'owner'
                GROUP BY mfid
                HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION 'resourceacl contains resources with multiple owner grants';
            END IF;
        END
        $$
    """)
    op.alter_column(
        "resourceacl",
        "permission",
        existing_type=sa.Enum(
            "viewer", "contributor", "editor", "admin", "owner",
            name="resource_role",
        ),
        nullable=False,
    )
    op.alter_column(
        "useraccessgrouplink",
        "role",
        existing_type=sa.Enum(
            "viewer", "contributor", "editor", "admin", "owner",
            name="resource_role",
        ),
        nullable=False,
    )
    op.create_index(
        "one_owner_acl_per_resource",
        "resourceacl",
        ["mfid"],
        unique=True,
        postgresql_where=sa.text("permission = 'owner'"),
    )


def downgrade() -> None:
    op.drop_index("one_owner_acl_per_resource", table_name="resourceacl")
    op.alter_column(
        "useraccessgrouplink",
        "role",
        existing_type=sa.Enum(
            "viewer", "contributor", "editor", "admin", "owner",
            name="resource_role",
        ),
        nullable=True,
    )
    op.alter_column(
        "resourceacl",
        "permission",
        existing_type=sa.Enum(
            "viewer", "contributor", "editor", "admin", "owner",
            name="resource_role",
        ),
        nullable=True,
    )

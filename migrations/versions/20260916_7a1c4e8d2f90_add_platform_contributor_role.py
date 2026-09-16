"""add platform contributor role

Revision ID: 7a1c4e8d2f90
Revises: 3f2b6a9c1d84
Create Date: 2026-09-16 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "7a1c4e8d2f90"
down_revision: Union[str, Sequence[str], None] = "3f2b6a9c1d84"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(sa.text(
            "ALTER TYPE platform_role ADD VALUE IF NOT EXISTS 'contributor' AFTER 'none'"
        ))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute(sa.text(
        "UPDATE \"user\" SET platform_role = 'none' WHERE platform_role = 'contributor'"
    ))
    op.execute(sa.text(
        "ALTER TABLE \"user\" ALTER COLUMN platform_role DROP DEFAULT"
    ))
    op.execute(sa.text("ALTER TYPE platform_role RENAME TO platform_role_old"))
    op.execute(sa.text(
        "CREATE TYPE platform_role AS ENUM ('none', 'support', 'admin')"
    ))
    op.execute(sa.text(
        "ALTER TABLE \"user\" ALTER COLUMN platform_role TYPE platform_role "
        "USING platform_role::text::platform_role"
    ))
    op.execute(sa.text(
        "ALTER TABLE \"user\" ALTER COLUMN platform_role SET DEFAULT 'none'"
    ))
    op.execute(sa.text("DROP TYPE platform_role_old"))

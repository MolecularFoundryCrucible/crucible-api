"""add project.unique_id

A real, independently-generated identifier for projects — a genuine MFID,
not project_id reused as one (which is what idtype.mfid does for projects
today). Purely additive: nothing reads this column yet. project_id remains
what ACL/group lookups key on and what Dataset.project_id/Sample.project_id
FK to; this is groundwork for the eventual MFID-named-groups work (group C),
not a behavior change. See docs/permission_system_plan.md, "Project
identity: mfid vs project_id".

Nullable on existing rows until the backfill runs:
    DB_CONF_FILE=.env python migrations/data/<date>_backfill_project_unique_id.py --commit

Revision ID: 4ae6482731ed
Revises: 1bb842c39928
Create Date: 2026-08-10 16:34:36.646300

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4ae6482731ed'
down_revision: Union[str, Sequence[str], None] = '1bb842c39928'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("project", sa.Column("unique_id", sa.String(), nullable=True))
    # Partial: skips NULLs so existing rows don't collide before the backfill
    # runs, matching add_username_to_user.sql's convention for the same case.
    op.create_index(
        "ix_project_unique_id", "project", ["unique_id"],
        unique=True, postgresql_where=sa.text("unique_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_project_unique_id", table_name="project")
    op.drop_column("project", "unique_id")

"""Track user, API key, project, and instrument activity."""
from alembic import op
import sqlalchemy as sa

revision = "b644cf2e7240"
down_revision = "d7b51bd885fc"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("user", sa.Column("last_active_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("apitoken", sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("project", sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("instrument", sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_user_last_active_at", "user", ["last_active_at"])
    op.create_index("ix_project_last_activity_at", "project", ["last_activity_at"])
    op.create_index("ix_instrument_last_activity_at", "instrument", ["last_activity_at"])


def downgrade():
    op.drop_index("ix_instrument_last_activity_at", table_name="instrument")
    op.drop_index("ix_project_last_activity_at", table_name="project")
    op.drop_index("ix_user_last_active_at", table_name="user")
    op.drop_column("instrument", "last_activity_at")
    op.drop_column("project", "last_activity_at")
    op.drop_column("apitoken", "last_used_at")
    op.drop_column("user", "last_active_at")

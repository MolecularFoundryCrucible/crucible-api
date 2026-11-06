"""Mark accounts that wait for an administrator's approval."""
import sqlalchemy as sa
from alembic import op

revision = "b4d7f2a9c615"
down_revision = "a8c2e5f1d903"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("user", sa.Column("pending_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_user_pending_at", "user", ["pending_at"], postgresql_where=sa.text("pending_at IS NOT NULL"))


def downgrade():
    op.drop_index("ix_user_pending_at", table_name="user")
    op.drop_column("user", "pending_at")

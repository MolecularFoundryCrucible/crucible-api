"""Add the api_key table, which holds API keys as hashes beside the plain apitoken table."""
import sqlalchemy as sa
from alembic import op

revision = "c9e1a4b7d362"
down_revision = "b4d7f2a9c615"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "api_key",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="CASCADE"), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("key_last4", sa.String(4), nullable=False),
        sa.Column("name", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_api_key_user_id", "api_key", ["user_id"])


def downgrade():
    op.drop_table("api_key")

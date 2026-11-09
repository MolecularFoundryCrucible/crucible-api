"""Add password_token, for one-time links to set or reset a password. It starts empty."""
import sqlalchemy as sa
from alembic import op

revision = "a7df9dd039cf"
down_revision = "1226f8497119"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "password_token",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="CASCADE"), nullable=False),
        sa.Column("purpose", sa.String(16), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("purpose IN ('setup', 'reset')", name="ck_password_token_purpose"),
    )
    op.create_index("uq_password_token_live_user", "password_token", ["user_id"], unique=True, postgresql_where=sa.text("consumed_at IS NULL AND voided_at IS NULL"))
    op.create_index("ix_password_token_expires_at", "password_token", ["expires_at"])


def downgrade():
    op.drop_index("ix_password_token_expires_at", table_name="password_token")
    op.drop_index("uq_password_token_live_user", table_name="password_token")
    op.drop_table("password_token")

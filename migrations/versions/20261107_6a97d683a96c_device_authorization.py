"""Add device_authorization, for device sign-ins (RFC 8628). It starts empty."""
import sqlalchemy as sa
from alembic import op

revision = "6a97d683a96c"
down_revision = "dc8e5f55644f"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "device_authorization",
        sa.Column("device_code_hash", sa.String(64), primary_key=True),
        sa.Column("user_code_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("client_id", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("interval", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="CASCADE"), nullable=True),
        sa.Column("auth_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("denied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_device_authorization_expires_at", "device_authorization", ["expires_at"])


def downgrade():
    op.drop_index("ix_device_authorization_expires_at", table_name="device_authorization")
    op.drop_table("device_authorization")

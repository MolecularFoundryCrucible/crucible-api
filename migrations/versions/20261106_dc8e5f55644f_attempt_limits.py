"""Count password failures per account and per client address."""
import sqlalchemy as sa
from alembic import op

revision = "dc8e5f55644f"
down_revision = "daa047f48e3b"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("local_credential", sa.Column("failed_attempts", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("local_credential", sa.Column("trusted_failed_attempts", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("local_credential", sa.Column("blocked_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("local_credential", sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "login_throttle",
        sa.Column("address_hash", sa.String(64), primary_key=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("blocked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_login_throttle_last_failure_at", "login_throttle", ["last_failure_at"])


def downgrade():
    op.drop_index("ix_login_throttle_last_failure_at", table_name="login_throttle")
    op.drop_table("login_throttle")
    for column in ("locked_at", "blocked_until", "trusted_failed_attempts", "failed_attempts"):
        op.drop_column("local_credential", column)

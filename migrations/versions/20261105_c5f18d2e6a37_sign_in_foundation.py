"""Add the sign-in event history, and columns for switching accounts off and for confirmed logins."""
import sqlalchemy as sa
from alembic import op

revision = "c5f18d2e6a37"
down_revision = "a3d7e1c94b28"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("user", sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("user_identity", sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "auth_event",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event", sa.String(32), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="SET NULL"), nullable=True),
        sa.Column("user_unique_id", sa.String(), nullable=True),
        sa.Column("actor_user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="SET NULL"), nullable=True),
        sa.Column("issuer", sa.Text(), nullable=True),
        sa.Column("client_address", sa.Text(), nullable=True),
    )
    op.create_index("ix_auth_event_at", "auth_event", ["at"])
    op.create_index("ix_auth_event_user_id_at", "auth_event", ["user_id", "at"])


def downgrade():
    op.drop_table("auth_event")
    op.drop_column("user_identity", "verified_at")
    op.drop_column("user", "disabled_at")

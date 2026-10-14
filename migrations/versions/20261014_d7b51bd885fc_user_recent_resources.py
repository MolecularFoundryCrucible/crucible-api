"""Add per-user recently viewed resources."""
from alembic import op
import sqlalchemy as sa

revision = "d7b51bd885fc"
down_revision = "5582366c0eb4"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "userrecentresource",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("resource_mfid", sa.String(), sa.ForeignKey("idtype.mfid", ondelete="CASCADE"), primary_key=True),
        sa.Column("viewed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_userrecentresource_user_viewed", "userrecentresource", ["user_id", "viewed_at"])


def downgrade():
    op.drop_index("ix_userrecentresource_user_viewed", table_name="userrecentresource")
    op.drop_table("userrecentresource")

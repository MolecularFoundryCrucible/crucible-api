"""Add local_credential: passwords, apart from the user table. It starts empty."""
import sqlalchemy as sa
from alembic import op

revision = "daa047f48e3b"
down_revision = "4a3a4e216223"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "local_credential",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("pepper_id", sa.String(32), nullable=True),
        sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("local_credential")

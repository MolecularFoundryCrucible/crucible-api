"""Add per-user preferences."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "5582366c0eb4"
down_revision = "f3b7c2a91d04"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "userpreferences",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("preferences", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("jsonb_typeof(preferences) = 'object'", name="ck_userpreferences_object"),
        sa.CheckConstraint("octet_length(preferences::text) <= 16384", name="ck_userpreferences_size"),
        sa.CheckConstraint("revision >= 1", name="ck_userpreferences_revision"),
    )


def downgrade():
    op.drop_table("userpreferences")

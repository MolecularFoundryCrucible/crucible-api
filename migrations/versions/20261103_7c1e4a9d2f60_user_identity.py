"""Add user_identity: which provider accounts belong to which user. The table starts empty."""
from alembic import op
import sqlalchemy as sa

revision = "7c1e4a9d2f60"
down_revision = "564d0bd33a5c"
branch_labels = None
depends_on = None

ORCID_ISSUERS = "issuer IN ('https://orcid.org', 'https://sandbox.orcid.org')"


def upgrade():
    op.create_table(
        "user_identity",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="CASCADE"), nullable=False),
        sa.Column("issuer", sa.Text(), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("label", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="SET NULL"), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("issuer", "subject", name="uq_user_identity_issuer_subject"),
    )
    op.create_index("ix_user_identity_user_id", "user_identity", ["user_id"])
    op.create_index("ux_user_identity_one_orcid", "user_identity", ["user_id"], unique=True, postgresql_where=sa.text(ORCID_ISSUERS))


def downgrade():
    op.drop_table("user_identity")

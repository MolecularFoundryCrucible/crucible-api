"""Let Hub login transactions and codes carry a purpose, so a transaction can link a login to an account."""
import sqlalchemy as sa
from alembic import op

revision = "a8c2e5f1d903"
down_revision = "f3a9d1c7b842"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column("hublogintransaction", "browser_secret_hash", existing_type=sa.String(64), nullable=True)
    op.add_column("hublogintransaction", sa.Column("purpose", sa.String(16), nullable=False, server_default="sign_in"))
    op.add_column("hublogintransaction", sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=True))
    op.add_column("hublogintransaction", sa.Column("start_hash", sa.String(64), nullable=True))
    op.create_index("ix_hublogintransaction_user_id", "hublogintransaction", ["user_id"])
    op.create_unique_constraint("uq_hublogintransaction_start_hash", "hublogintransaction", ["start_hash"])
    op.add_column("hublogincode", sa.Column("purpose", sa.String(16), nullable=False, server_default="sign_in"))
    op.add_column("hublogincode", sa.Column("issuer", sa.Text(), nullable=True))
    op.add_column("hublogincode", sa.Column("subject", sa.String(255), nullable=True))
    op.add_column("hublogincode", sa.Column("label", sa.String(255), nullable=True))


def downgrade():
    op.execute(sa.text("DELETE FROM hublogincode WHERE purpose <> 'sign_in'"))
    op.execute(sa.text("DELETE FROM hublogintransaction WHERE purpose <> 'sign_in' OR browser_secret_hash IS NULL"))
    for column in ("label", "subject", "issuer", "purpose"):
        op.drop_column("hublogincode", column)
    op.drop_constraint("uq_hublogintransaction_start_hash", "hublogintransaction", type_="unique")
    op.drop_index("ix_hublogintransaction_user_id", table_name="hublogintransaction")
    for column in ("start_hash", "user_id", "purpose"):
        op.drop_column("hublogintransaction", column)
    op.alter_column("hublogintransaction", "browser_secret_hash", existing_type=sa.String(64), nullable=False)

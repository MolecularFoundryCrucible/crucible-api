"""Record which provider each Hub sign-in started with."""
import sqlalchemy as sa
from alembic import op

revision = "a3d7e1c94b28"
down_revision = "7c1e4a9d2f60"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("hublogintransaction", sa.Column("provider_key", sa.String(length=32), nullable=True))
    op.add_column("hublogintransaction", sa.Column("provider_code_verifier", sa.String(length=128), nullable=True))
    op.add_column("hublogintransaction", sa.Column("required_user", sa.String(length=255), nullable=True))


def downgrade():
    op.drop_column("hublogintransaction", "required_user")
    op.drop_column("hublogintransaction", "provider_code_verifier")
    op.drop_column("hublogintransaction", "provider_key")

"""Record which session started a link, so only that session can confirm it."""
import sqlalchemy as sa
from alembic import op

revision = "15eb11ce75fd"
down_revision = "2b0ab3a4520a"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("hublogintransaction", "hublogincode"):
        op.add_column(table, sa.Column("session_family_id", sa.String(64), nullable=True))


def downgrade():
    for table in ("hublogincode", "hublogintransaction"):
        op.drop_column(table, "session_family_id")

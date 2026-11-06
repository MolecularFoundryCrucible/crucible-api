"""Record which client a sign-in or link came through."""
import sqlalchemy as sa
from alembic import op

revision = "4a3a4e216223"
down_revision = "15eb11ce75fd"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("auth_event", sa.Column("client_id", sa.String(100), nullable=True))


def downgrade():
    op.drop_column("auth_event", "client_id")

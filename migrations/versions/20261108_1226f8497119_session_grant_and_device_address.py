"""Record which grant started each session, and where each device sign-in was asked from. Both
columns are new and nullable: existing sessions count as browser sign-ins."""
import sqlalchemy as sa
from alembic import op

revision = "1226f8497119"
down_revision = "6a97d683a96c"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("hubtokenfamily", sa.Column("grant_type", sa.String(64), nullable=True))
    op.add_column("device_authorization", sa.Column("requested_from", sa.String(45), nullable=True))


def downgrade():
    op.drop_column("device_authorization", "requested_from")
    op.drop_column("hubtokenfamily", "grant_type")

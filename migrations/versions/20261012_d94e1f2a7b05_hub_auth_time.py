"""Record the ORCID authentication time on Hub login codes and token families."""
from alembic import op
import sqlalchemy as sa

revision = "d94e1f2a7b05"
down_revision = "c83a7d014b29"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("hublogincode", sa.Column("auth_time", sa.DateTime(timezone=True), nullable=True))
    op.add_column("hubtokenfamily", sa.Column("auth_time", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.drop_column("hubtokenfamily", "auth_time")
    op.drop_column("hublogincode", "auth_time")

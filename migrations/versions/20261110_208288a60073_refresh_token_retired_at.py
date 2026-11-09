"""Add hubrefreshtoken.retired_at, set when a refresh token is retired because a sibling was used.
New and nullable: existing tokens count as never retired."""
import sqlalchemy as sa
from alembic import op

revision = "208288a60073"
down_revision = "a7df9dd039cf"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("hubrefreshtoken", sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.drop_column("hubrefreshtoken", "retired_at")

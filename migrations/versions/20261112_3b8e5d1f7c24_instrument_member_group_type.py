"""Add the instrument_member access group type, for the people who are members of an instrument.
Postgres cannot drop an enum value, so downgrade leaves it in place."""
import sqlalchemy as sa
from alembic import op

revision = "3b8e5d1f7c24"
down_revision = "c41d7e9a52b6"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.execute(sa.text("ALTER TYPE access_group_type ADD VALUE IF NOT EXISTS 'instrument_member' AFTER 'instrument'"))


def downgrade():
    pass

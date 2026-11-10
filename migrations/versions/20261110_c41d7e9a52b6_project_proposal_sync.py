"""Add project.proposal_sync: whether people named on a matching proposal are added to the project.
Existing projects keep it on; new rows default to off."""
import sqlalchemy as sa
from alembic import op

revision = "c41d7e9a52b6"
down_revision = "208288a60073"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("project", sa.Column("proposal_sync", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.alter_column("project", "proposal_sync", server_default=sa.false())


def downgrade():
    op.drop_column("project", "proposal_sync")

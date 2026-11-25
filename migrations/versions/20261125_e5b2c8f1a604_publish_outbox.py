"""Record when an ingestion request or postprocessing run reached the message queue, so unsent work is sent again."""
from alembic import op
import sqlalchemy as sa

revision = "e5b2c8f1a604"
down_revision = "c4d1e7a9b203"
branch_labels = None
depends_on = None

TABLES = ("ingestionrequest", "postprocessingrun")


def upgrade():
    for table in TABLES:
        op.add_column(table, sa.Column("published_at", sa.DateTime(timezone=True), nullable=True))
        # Work from before this column was either sent or recorded as failed; none of it is sent again.
        op.execute(sa.text(f"UPDATE {table} SET published_at = CURRENT_TIMESTAMP"))
    op.create_index("ix_ingestionrequest_unpublished", "ingestionrequest", ["id"], postgresql_where=sa.text("published_at IS NULL AND status = 'requested'"))
    op.create_index("ix_postprocessingrun_unpublished", "postprocessingrun", ["id"], postgresql_where=sa.text("published_at IS NULL AND status = 'requested'"))


def downgrade():
    op.drop_index("ix_postprocessingrun_unpublished", table_name="postprocessingrun")
    op.drop_index("ix_ingestionrequest_unpublished", table_name="ingestionrequest")
    for table in TABLES:
        op.drop_column(table, "published_at")

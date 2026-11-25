"""Add workers: what each running worker can do, renewed by heartbeat."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "c4d1e7a9b203"
down_revision = "afcc2eef5a87"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "workers",
        sa.Column("worker_id", sa.String(), primary_key=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("capabilities", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
        sa.Column("buckets", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
        sa.Column("worker_version", sa.String(), nullable=True),
        sa.Column("package_version", sa.String(), nullable=True),
        sa.Column("registered_by", sa.String(), nullable=False),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_workers_kind_last_seen", "workers", ["kind", "last_seen_at"])


def downgrade():
    op.drop_index("ix_workers_kind_last_seen", table_name="workers")
    op.drop_table("workers")

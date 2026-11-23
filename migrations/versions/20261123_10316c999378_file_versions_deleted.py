"""Let file versions hold deleted files: a filename, the file's ingestion requests and the reason 'deleted'."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "10316c999378"
down_revision = "b0e044b67c87"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("file_versions", sa.Column("filename", sa.String(), nullable=True))
    op.add_column("file_versions", sa.Column("ingestion_requests", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=True))
    op.drop_constraint("ck_file_versions_reason", "file_versions", type_="check")
    op.create_check_constraint("ck_file_versions_reason", "file_versions", "reason IN ('replaced', 'deleted')")


def downgrade():
    op.execute("UPDATE file_versions SET reason = 'replaced' WHERE reason = 'deleted'")
    op.drop_constraint("ck_file_versions_reason", "file_versions", type_="check")
    op.create_check_constraint("ck_file_versions_reason", "file_versions", "reason IN ('replaced')")
    op.drop_column("file_versions", "ingestion_requests")
    op.drop_column("file_versions", "filename")

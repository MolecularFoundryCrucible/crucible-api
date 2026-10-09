"""Add multipart scheduling and checksum fields."""
from alembic import op
import sqlalchemy as sa

revision = "c83a7d014b29"
down_revision = "b72f6c903a18"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("upload_sessions", sa.Column("claimed_crc32c", sa.String(), nullable=True))
    op.add_column("upload_sessions", sa.Column("attempts", sa.Integer(), nullable=True))
    for name in ("next_attempt_at", "cleaned_at", "part_signing_after"):
        op.add_column("upload_sessions", sa.Column(name, sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_upload_sessions_work", "upload_sessions", ["protocol", "next_attempt_at"])


def downgrade():
    op.drop_index("ix_upload_sessions_work", table_name="upload_sessions")
    for name in ("part_signing_after", "cleaned_at", "next_attempt_at", "attempts", "claimed_crc32c"):
        op.drop_column("upload_sessions", name)

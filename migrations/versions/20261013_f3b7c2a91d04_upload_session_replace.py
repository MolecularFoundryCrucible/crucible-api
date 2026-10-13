"""Record the target file and base ETag of multipart replace sessions."""
from alembic import op
import sqlalchemy as sa

revision = "f3b7c2a91d04"
down_revision = "e5a1c8d93f27"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("upload_sessions", sa.Column("replace_file_mfid", sa.String(), nullable=True))
    op.add_column("upload_sessions", sa.Column("replace_base_etag", sa.String(), nullable=True))
    op.create_index("ix_upload_sessions_replace_file_mfid", "upload_sessions", ["replace_file_mfid"])


def downgrade():
    op.drop_index("ix_upload_sessions_replace_file_mfid", table_name="upload_sessions")
    op.drop_column("upload_sessions", "replace_base_etag")
    op.drop_column("upload_sessions", "replace_file_mfid")

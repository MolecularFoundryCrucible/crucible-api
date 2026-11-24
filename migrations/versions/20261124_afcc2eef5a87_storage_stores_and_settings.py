"""Record the store of each file, file version and upload session, and add record settings."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "afcc2eef5a87"
down_revision = "10316c999378"
branch_labels = None
depends_on = None

STORED = ("associatedfile", "file_versions", "upload_sessions")
WAITING = "deleted_at IS NULL AND restored_at IS NULL"


def upgrade():
    for table in STORED:
        op.add_column(table, sa.Column("store_id", sa.String(), nullable=True))
    # Two stores may hold the same bucket and key, so a waiting version is unique per path within its store.
    op.drop_index("uq_file_versions_waiting_path", table_name="file_versions")
    op.create_index("uq_file_versions_waiting_path", "file_versions", ["storage_path", "store_id"], unique=True, postgresql_where=sa.text(WAITING))
    op.create_table(
        "resource_settings",
        sa.Column("mfid", sa.String(), sa.ForeignKey("idtype.mfid", ondelete="CASCADE"), primary_key=True),
        sa.Column("settings", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_index("uq_file_versions_waiting_path", table_name="file_versions")
    op.create_index("uq_file_versions_waiting_path", "file_versions", ["storage_path"], unique=True, postgresql_where=sa.text(WAITING))
    op.drop_table("resource_settings")
    for table in STORED:
        op.drop_column(table, "store_id")

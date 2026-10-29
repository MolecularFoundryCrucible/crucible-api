"""Add file versions: replaced file content kept for a grace period, then deleted from storage."""
from alembic import op
import sqlalchemy as sa

revision = "564d0bd33a5c"
down_revision = "b8f2d6e41c07"
branch_labels = None
depends_on = None

WAITING = "deleted_at IS NULL AND restored_at IS NULL"


def upgrade():
    op.create_table(
        "file_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("file_mfid", sa.String(), nullable=False),
        sa.Column("dataset_mfid", sa.String(), nullable=False),
        sa.Column("storage_path", sa.String(), nullable=False),
        sa.Column("storage_generation", sa.String(), nullable=True),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("sha256_hash", sa.String(), nullable=False),
        sa.Column("storage_crc32c", sa.String(), nullable=True),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delete_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("restored_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure_code", sa.String(), nullable=True),
        sa.CheckConstraint("reason IN ('replaced')", name="ck_file_versions_reason"),
    )
    op.create_index("ix_file_versions_file_mfid", "file_versions", ["file_mfid"])
    op.create_index("ix_file_versions_due", "file_versions", ["delete_after"], postgresql_where=sa.text(WAITING))
    op.create_index("uq_file_versions_waiting_path", "file_versions", ["storage_path"], unique=True, postgresql_where=sa.text(WAITING))


def downgrade():
    op.drop_index("uq_file_versions_waiting_path", table_name="file_versions")
    op.drop_index("ix_file_versions_due", table_name="file_versions")
    op.drop_index("ix_file_versions_file_mfid", table_name="file_versions")
    op.drop_table("file_versions")

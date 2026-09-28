"""Add the multipart upload foundation to existing tables."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "b72f6c903a18"
down_revision = "e92a6b1c730f"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("upload_sessions", sa.Column("protocol", sa.String(), nullable=True))
    op.add_column("upload_sessions", sa.Column("bucket", sa.String(), nullable=True))
    op.add_column("upload_sessions", sa.Column("provider_upload_id", sa.String(), nullable=True))
    op.add_column("upload_sessions", sa.Column("state", sa.String(), nullable=True))
    op.add_column("upload_sessions", sa.Column("part_size", sa.BigInteger(), nullable=True))
    op.add_column("upload_sessions", sa.Column("part_count", sa.Integer(), nullable=True))
    op.add_column("upload_sessions", sa.Column("idempotency_key", sa.String(), nullable=True))
    op.add_column("upload_sessions", sa.Column("request_fingerprint", sa.String(), nullable=True))
    op.add_column("upload_sessions", sa.Column("claimed_sha256", sa.String(), nullable=True))
    op.add_column("upload_sessions", sa.Column("result_file_mfid", sa.String(), nullable=True))
    op.add_column("upload_sessions", sa.Column("storage_generation", sa.String(), nullable=True))
    op.add_column("upload_sessions", sa.Column("failure_code", sa.String(), nullable=True))
    op.add_column("upload_sessions", sa.Column("lease_owner", sa.String(), nullable=True))
    op.add_column("upload_sessions", sa.Column("version", sa.Integer(), nullable=True))
    op.add_column("upload_sessions", sa.Column("part_manifest", postgresql.JSONB(), nullable=True))
    op.add_column("upload_sessions", sa.Column("created_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("upload_sessions", sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("upload_sessions", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("upload_sessions", sa.Column("cleanup_requested_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("associatedfile", sa.Column("storage_generation", sa.String(), nullable=True))
    op.add_column("associatedfile", sa.Column("storage_crc32c", sa.String(), nullable=True))
    op.add_column("associatedfile", sa.Column("sha256_verified_at", sa.DateTime(timezone=True), nullable=True))
    op.create_unique_constraint("uq_upload_session_idempotency", "upload_sessions", ["owner_user_id", "dataset_mfid", "protocol", "idempotency_key"])
    op.create_check_constraint("ck_upload_session_protocol_state", "upload_sessions", "protocol IS NULL OR (protocol = 'gcs_xml_v1' AND state IS NOT NULL AND state IN ('initiating', 'uploading', 'finalizing', 'verifying', 'completed', 'aborting', 'aborted', 'expired', 'failed'))")
    op.create_check_constraint("ck_upload_session_part_size", "upload_sessions", 'part_size IS NULL OR part_size > 0')
    op.create_check_constraint("ck_upload_session_part_count", "upload_sessions", 'part_count IS NULL OR (part_count BETWEEN 1 AND 10000)')


def downgrade():
    op.drop_constraint("ck_upload_session_protocol_state", "upload_sessions", type_="check")
    op.drop_constraint("ck_upload_session_part_size", "upload_sessions", type_="check")
    op.drop_constraint("ck_upload_session_part_count", "upload_sessions", type_="check")
    op.drop_constraint("uq_upload_session_idempotency", "upload_sessions", type_="unique")
    op.drop_column("associatedfile", "sha256_verified_at")
    op.drop_column("associatedfile", "storage_crc32c")
    op.drop_column("associatedfile", "storage_generation")
    op.drop_column("upload_sessions", "cleanup_requested_at")
    op.drop_column("upload_sessions", "lease_expires_at")
    op.drop_column("upload_sessions", "updated_at")
    op.drop_column("upload_sessions", "created_at")
    op.drop_column("upload_sessions", "part_manifest")
    op.drop_column("upload_sessions", "version")
    op.drop_column("upload_sessions", "lease_owner")
    op.drop_column("upload_sessions", "failure_code")
    op.drop_column("upload_sessions", "storage_generation")
    op.drop_column("upload_sessions", "result_file_mfid")
    op.drop_column("upload_sessions", "claimed_sha256")
    op.drop_column("upload_sessions", "request_fingerprint")
    op.drop_column("upload_sessions", "idempotency_key")
    op.drop_column("upload_sessions", "part_count")
    op.drop_column("upload_sessions", "part_size")
    op.drop_column("upload_sessions", "state")
    op.drop_column("upload_sessions", "provider_upload_id")
    op.drop_column("upload_sessions", "bucket")
    op.drop_column("upload_sessions", "protocol")

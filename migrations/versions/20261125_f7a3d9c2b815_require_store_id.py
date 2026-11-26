"""Require a store on every stored file, file version and upload session."""
from alembic import op
import sqlalchemy as sa

revision = "f7a3d9c2b815"
down_revision = "e5b2c8f1a604"
branch_labels = None
depends_on = None

UNASSIGNED = {
    "associatedfile": "storage_backend = 'gcs' AND store_id IS NULL",
    "file_versions": "store_id IS NULL",
    "upload_sessions": "store_id IS NULL",
}


def upgrade():
    connection = op.get_bind()
    missing = {table: connection.execute(sa.text(f"SELECT count(*) FROM {table} WHERE {where}")).scalar() for table, where in UNASSIGNED.items()}
    if any(missing.values()):
        counts = ", ".join(f"{table} {count}" for table, count in missing.items() if count)
        raise RuntimeError(f"Rows without a store ({counts}): run python -m app.cli files assign-store --store <id> --commit with crucible-api 4.1.x before upgrading")
    op.create_check_constraint("ck_associatedfile_stored_has_store", "associatedfile", "storage_backend <> 'gcs' OR store_id IS NOT NULL")
    for table in ("file_versions", "upload_sessions"):
        op.alter_column(table, "store_id", existing_type=sa.String(), nullable=False)


def downgrade():
    for table in ("file_versions", "upload_sessions"):
        op.alter_column(table, "store_id", existing_type=sa.String(), nullable=True)
    op.drop_constraint("ck_associatedfile_stored_has_store", "associatedfile", type_="check")

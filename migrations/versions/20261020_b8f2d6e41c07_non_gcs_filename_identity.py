"""Enforce canonical, unique filenames for non-GCS file records.

Adapted from PR #335 (Peter Doak). Stops instead of choosing a survivor when existing non-GCS rows
violate the rules; production had no non-GCS rows when this was written (2026-10-20).
"""
import sqlalchemy as sa
from alembic import op

revision = "b8f2d6e41c07"
down_revision = "9d41c6a2e7f3"
branch_labels = None
depends_on = None

PATH_CHECK = (
    "storage_backend = 'gcs' OR ("
    "filename <> '' AND filename NOT LIKE '/%' AND filename NOT LIKE '%/' "
    "AND filename NOT LIKE '%//%' AND filename NOT IN ('.', '..') "
    "AND filename NOT LIKE './%' AND filename NOT LIKE '../%' "
    "AND filename NOT LIKE '%/./%' AND filename NOT LIKE '%/../%' "
    "AND filename NOT LIKE '%/.' AND filename NOT LIKE '%/..')"
)
CONTROLS_CHECK = "storage_backend = 'gcs' OR filename !~ E'[\\x01-\\x1F\\x7F]'"


def upgrade():
    connection = op.get_bind()
    invalid = connection.execute(sa.text(f"SELECT count(*) FROM associatedfile WHERE NOT ({PATH_CHECK}) OR NOT ({CONTROLS_CHECK})")).scalar()
    if invalid:
        raise RuntimeError(f"{invalid} non-GCS file records have non-canonical filenames; repair them before this migration")
    duplicates = connection.execute(sa.text(
        "SELECT count(*) FROM (SELECT 1 FROM associatedfile WHERE storage_backend <> 'gcs' GROUP BY dataset_id, filename HAVING count(*) > 1) d"
    )).scalar()
    if duplicates:
        raise RuntimeError(f"{duplicates} (dataset, filename) pairs have more than one non-GCS record; resolve them before this migration")
    op.create_check_constraint("ck_associatedfile_non_gcs_filename_path", "associatedfile", PATH_CHECK)
    op.create_check_constraint("ck_associatedfile_non_gcs_filename_controls", "associatedfile", CONTROLS_CHECK)
    op.create_index(
        "uq_associatedfile_non_gcs_dataset_filename", "associatedfile", ["dataset_id", "filename"], unique=True,
        postgresql_where=sa.text("storage_backend <> 'gcs'"),
    )


def downgrade():
    op.drop_index("uq_associatedfile_non_gcs_dataset_filename", table_name="associatedfile")
    op.drop_constraint("ck_associatedfile_non_gcs_filename_controls", "associatedfile", type_="check")
    op.drop_constraint("ck_associatedfile_non_gcs_filename_path", "associatedfile", type_="check")

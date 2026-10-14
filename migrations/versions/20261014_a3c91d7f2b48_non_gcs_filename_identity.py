"""enforce non-GCS dataset filename identity

Revision ID: a3c91d7f2b48
Revises: f3b7c2a91d04
Create Date: 2026-10-14 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "a3c91d7f2b48"
down_revision = "f3b7c2a91d04"
branch_labels = None
depends_on = None


_FILENAME_CHECK = (
    "storage_backend = 'gcs' OR ("
    "filename <> '' AND filename NOT LIKE '/%' AND filename NOT LIKE '%/' "
    "AND filename NOT LIKE '%//%' AND filename NOT IN ('.', '..') "
    "AND filename NOT LIKE './%' AND filename NOT LIKE '../%' "
    "AND filename NOT LIKE '%/./%' AND filename NOT LIKE '%/../%' "
    "AND filename NOT LIKE '%/.' AND filename NOT LIKE '%/..')"
)


def upgrade():
    # Stop rather than selecting an arbitrary survivor for legacy rows that
    # cannot satisfy the filename identity contract.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM associatedfile
                WHERE storage_backend <> 'gcs'
                  AND (
                      filename IS NULL OR filename = '' OR filename LIKE '/%'
                      OR filename LIKE '%/' OR filename LIKE '%//%'
                      OR filename IN ('.', '..') OR filename LIKE './%'
                      OR filename LIKE '../%' OR filename LIKE '%/./%'
                      OR filename LIKE '%/../%' OR filename LIKE '%/.'
                      OR filename LIKE '%/..' OR filename ~ E'[\\x01-\\x1F\\x7F]'
                  )
            ) THEN
                RAISE EXCEPTION
                    'Cannot enforce non-GCS filename identity: invalid filenames exist; audit and repair explicitly first';
            END IF;
            IF EXISTS (
                SELECT 1
                FROM associatedfile
                WHERE storage_backend <> 'gcs'
                GROUP BY dataset_id, filename
                HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION
                    'Cannot enforce non-GCS filename identity: duplicate (dataset_id, filename) values exist; audit and repair explicitly first';
            END IF;
        END $$;
        """
    )
    op.create_check_constraint(
        "ck_associatedfile_non_gcs_filename_path",
        "associatedfile",
        _FILENAME_CHECK,
    )
    op.create_check_constraint(
        "ck_associatedfile_non_gcs_filename_controls",
        "associatedfile",
        "storage_backend = 'gcs' OR filename !~ E'[\\x01-\\x1F\\x7F]'",
    )
    op.create_index(
        "uq_associatedfile_non_gcs_dataset_filename",
        "associatedfile",
        ["dataset_id", "filename"],
        unique=True,
        postgresql_where=sa.text("storage_backend <> 'gcs'"),
        sqlite_where=sa.text("storage_backend <> 'gcs'"),
    )


def downgrade():
    op.drop_index("uq_associatedfile_non_gcs_dataset_filename", table_name="associatedfile")
    op.drop_constraint("ck_associatedfile_non_gcs_filename_controls", "associatedfile", type_="check")
    op.drop_constraint("ck_associatedfile_non_gcs_filename_path", "associatedfile", type_="check")

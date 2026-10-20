"""Drop foreign keys on legacy slug and integer link columns.

dataset.project_id and userprojectlink.project_id referenced project.project_id without ON UPDATE,
so renaming a project failed. dataset.instrument_id duplicated instrument_mfid. The code reads and
writes only the MFID columns; the legacy columns and table are dropped in a later revision.
"""
from alembic import op

revision = "9d41c6a2e7f3"
down_revision = "5be2f0c8d4a1"
branch_labels = None
depends_on = None

FOREIGN_KEYS = (
    ("dataset", "dataset_project_id_fkey", "project_id", "project", "project_id"),
    ("userprojectlink", "userprojectlink_project_id_fkey", "project_id", "project", "project_id"),
    ("dataset", "dataset_instrument_id_fkey", "instrument_id", "instrument", "id"),
)


def upgrade():
    for table, name, *_ in FOREIGN_KEYS:
        op.execute(f'ALTER TABLE "{table}" DROP CONSTRAINT IF EXISTS "{name}"')


def downgrade():
    for table, name, column, target, target_column in FOREIGN_KEYS:
        op.execute(f'ALTER TABLE "{table}" ADD CONSTRAINT "{name}" FOREIGN KEY ("{column}") REFERENCES "{target}" ("{target_column}") NOT VALID')

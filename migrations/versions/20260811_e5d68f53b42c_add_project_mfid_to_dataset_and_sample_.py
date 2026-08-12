"""add project_mfid to dataset and sample, instrument_mfid to dataset

Repoints Dataset/Sample's project reference from the mutable Project.project_id
label onto the immutable Project.unique_id (MFID), and Dataset's instrument
reference onto Instrument.unique_id — same principle, same session, see
docs/permission_system_plan.md "Project identity: mfid vs project_id".

Additive only: project_id/instrument_name stay, nothing is dropped yet.
sample.project_mfid is Sample's first-ever project FK (project_id itself has
never had one) — see migrations/data/20260811_backfill_project_mfid.py for
why that column allows NULLs (pre-existing dangling Sample.project_id values
that don't match any real project).

Prerequisite fix folded in here: a Postgres FK requires the referenced
column to carry a *full* unique constraint, not just any unique index.
project.unique_id only had a partial one (`WHERE unique_id IS NOT NULL`,
from when NULLs were still expected pre-backfill), and instrument.unique_id
had no uniqueness enforcement at all. Both get a real unique index before
the FKs are created. Safe now that both are fully backfilled and NULL-free
(verified on staging before writing this).

Revision ID: e5d68f53b42c
Revises: 8ca4a71f1bed
Create Date: 2026-08-11 19:04:15.238695

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5d68f53b42c'
down_revision: Union[str, Sequence[str], None] = '8ca4a71f1bed'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("ix_project_unique_id", table_name="project")
    op.create_index("ix_project_unique_id", "project", ["unique_id"], unique=True)

    op.create_index("ix_instrument_unique_id", "instrument", ["unique_id"], unique=True)

    op.add_column("dataset", sa.Column("project_mfid", sa.String(), nullable=True))
    op.create_index("ix_dataset_project_mfid", "dataset", ["project_mfid"])
    op.create_foreign_key(
        "fk_dataset_project_mfid_project", "dataset", "project",
        ["project_mfid"], ["unique_id"],
    )

    op.add_column("dataset", sa.Column("instrument_mfid", sa.String(), nullable=True))
    op.create_index("ix_dataset_instrument_mfid", "dataset", ["instrument_mfid"])
    op.create_foreign_key(
        "fk_dataset_instrument_mfid_instrument", "dataset", "instrument",
        ["instrument_mfid"], ["unique_id"],
    )

    op.add_column("sample", sa.Column("project_mfid", sa.String(), nullable=True))
    op.create_index("ix_sample_project_mfid", "sample", ["project_mfid"])
    op.create_foreign_key(
        "fk_sample_project_mfid_project", "sample", "project",
        ["project_mfid"], ["unique_id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_sample_project_mfid_project", "sample", type_="foreignkey")
    op.drop_index("ix_sample_project_mfid", table_name="sample")
    op.drop_column("sample", "project_mfid")

    op.drop_constraint("fk_dataset_instrument_mfid_instrument", "dataset", type_="foreignkey")
    op.drop_index("ix_dataset_instrument_mfid", table_name="dataset")
    op.drop_column("dataset", "instrument_mfid")

    op.drop_constraint("fk_dataset_project_mfid_project", "dataset", type_="foreignkey")
    op.drop_index("ix_dataset_project_mfid", table_name="dataset")
    op.drop_column("dataset", "project_mfid")

    op.drop_index("ix_instrument_unique_id", table_name="instrument")

    op.drop_index("ix_project_unique_id", table_name="project")
    op.create_index(
        "ix_project_unique_id", "project", ["unique_id"],
        unique=True, postgresql_where=sa.text("unique_id IS NOT NULL"),
    )

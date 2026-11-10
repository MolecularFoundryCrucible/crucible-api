"""drop accessgroup owner_orcid and resource_mfid

Reverts 20260810_8ca4a71f1bed. Added to classify each access group by kind
(personal vs. project/instrument's own), but turned out to have exactly one
real reader (reassign_resource_project's previous_project_id, now resolved
via a live join through Project instead) and no other live consumer,
owner_orcid was written at creation and never read anywhere. Built ahead of
an actual need rather than in response to one, and the one real use doesn't
need a denormalized column. See docs/permissions.md.

Already applied and backfilled on staging (20260810_backfill_accessgroup_kind.py,
now deleted) — this is a genuine forward revert, not an edit of a migration
that never shipped, unlike the 20260811_e5d68f53b42c fix earlier this week.

Revision ID: bb780697b63b
Revises: e5d68f53b42c
Create Date: 2026-08-13 10:01:48.765195

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'bb780697b63b'
down_revision: Union[str, Sequence[str], None] = 'e5d68f53b42c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("ck_accessgroup_kind_exclusive", "accessgroup", type_="check")
    op.drop_constraint("fk_accessgroup_resource_mfid_idtype", "accessgroup", type_="foreignkey")
    op.drop_constraint("fk_accessgroup_owner_orcid_user", "accessgroup", type_="foreignkey")
    op.drop_index("ix_accessgroup_resource_mfid", table_name="accessgroup")
    op.drop_index("ix_accessgroup_owner_orcid", table_name="accessgroup")
    op.drop_column("accessgroup", "resource_mfid")
    op.drop_column("accessgroup", "owner_orcid")


def downgrade() -> None:
    import sqlalchemy as sa

    op.add_column("accessgroup", sa.Column("owner_orcid", sa.String(), nullable=True))
    op.add_column("accessgroup", sa.Column("resource_mfid", sa.String(), nullable=True))
    op.create_index("ix_accessgroup_owner_orcid", "accessgroup", ["owner_orcid"])
    op.create_index("ix_accessgroup_resource_mfid", "accessgroup", ["resource_mfid"])
    op.create_foreign_key(
        "fk_accessgroup_owner_orcid_user", "accessgroup", "user",
        ["owner_orcid"], ["orcid"],
    )
    op.create_foreign_key(
        "fk_accessgroup_resource_mfid_idtype", "accessgroup", "idtype",
        ["resource_mfid"], ["mfid"],
    )
    op.create_check_constraint(
        "ck_accessgroup_kind_exclusive", "accessgroup",
        "NOT (owner_orcid IS NOT NULL AND resource_mfid IS NOT NULL)",
    )

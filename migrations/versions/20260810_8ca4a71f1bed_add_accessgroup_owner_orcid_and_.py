"""add accessgroup owner_orcid and resource_mfid

Classifies each access group by kind, instead of inferring it from
group_name. owner_orcid marks a personal ORCID group; resource_mfid marks a
project's or instrument's own group. Mutually exclusive by a CHECK
constraint — reserved groups (admin, public) and orphans have neither set.
See docs/permissions.md, "Membership".

Nullable on existing rows until the backfill runs:
    DB_CONF_FILE=.env python migrations/data/<date>_backfill_accessgroup_kind.py --commit

Revision ID: 8ca4a71f1bed
Revises: 79270728e3eb
Create Date: 2026-08-10 18:39:14.841096

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8ca4a71f1bed'
down_revision: Union[str, Sequence[str], None] = '79270728e3eb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
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


def downgrade() -> None:
    op.drop_constraint("ck_accessgroup_kind_exclusive", "accessgroup", type_="check")
    op.drop_constraint("fk_accessgroup_resource_mfid_idtype", "accessgroup", type_="foreignkey")
    op.drop_constraint("fk_accessgroup_owner_orcid_user", "accessgroup", type_="foreignkey")
    op.drop_index("ix_accessgroup_resource_mfid", table_name="accessgroup")
    op.drop_index("ix_accessgroup_owner_orcid", table_name="accessgroup")
    op.drop_column("accessgroup", "resource_mfid")
    op.drop_column("accessgroup", "owner_orcid")

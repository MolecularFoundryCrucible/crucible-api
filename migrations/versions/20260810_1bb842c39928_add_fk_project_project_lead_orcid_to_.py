"""add FK project.project_lead_orcid to user.orcid

project_lead_orcid was an unconstrained string despite always naming a real
user — the same class of gap docs/schema_hygiene.md flags for owner_orcid,
just never closed here. It's staying permanently as a denormalized display
pointer (see docs/permission_system_plan.md, "Project lead becomes a role" —
superseded from an earlier plan to drop the column in phase 5), so it's worth
the same integrity guarantee AccessGroupJoinRequest.requester_id already has.

Run this precondition check first — it must return zero rows, or the ADD
CONSTRAINT below fails:

    SELECT project_id, project_lead_orcid FROM project p
    WHERE NOT EXISTS (SELECT 1 FROM "user" u WHERE u.orcid = p.project_lead_orcid);

Revision ID: 1bb842c39928
Revises: fc9be1ddfb92
Create Date: 2026-08-10 15:01:33.103882

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1bb842c39928'
down_revision: Union[str, Sequence[str], None] = 'fc9be1ddfb92'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_foreign_key(
        "fk_project_project_lead_orcid_user",
        source_table="project",
        referent_table="user",
        local_cols=["project_lead_orcid"],
        remote_cols=["orcid"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_project_project_lead_orcid_user", "project", type_="foreignkey",
    )

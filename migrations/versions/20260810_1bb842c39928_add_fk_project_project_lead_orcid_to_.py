"""Add FK from project.project_lead_orcid to user.orcid.

Legacy projects may name leads who do not have a current user row. PostgreSQL's
NOT VALID constraint preserves those rows while enforcing the relationship for
all new and updated values. The constraint can be validated after legacy leads
are reconciled.

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
        postgresql_not_valid=True,
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_project_project_lead_orcid_user", "project", type_="foreignkey",
    )

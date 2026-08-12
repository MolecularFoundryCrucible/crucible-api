"""baseline: adopt alembic, schema as of 2026-08-10

Deliberately empty. Staging and production already carry this schema, built
up through 21 one-off scripts now archived in migrations/data/ (see that
folder and docs/permission_migration_runbook.md) — there is nothing to gain
by replaying that history through Alembic's upgrade/downgrade machinery.

Every real database gets marked as already being here without running
anything:

    DB_CONF_FILE=.env alembic stamp head

From this point on, schema changes are real Alembic revisions:
`alembic revision --autogenerate -m "..."`, reviewed, then
`alembic upgrade head` — staging first, then production with
`-x yes-this-is-production=true`.

Revision ID: fc9be1ddfb92
Revises:
Create Date: 2026-08-10 13:27:46.496722

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'fc9be1ddfb92'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass

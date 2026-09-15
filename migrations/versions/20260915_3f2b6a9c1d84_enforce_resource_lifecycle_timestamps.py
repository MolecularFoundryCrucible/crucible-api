"""enforce resource lifecycle timestamps

Revision ID: 3f2b6a9c1d84
Revises: 8c4f2a1d7e60
Create Date: 2026-09-15 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "3f2b6a9c1d84"
down_revision: Union[str, Sequence[str], None] = "8c4f2a1d7e60"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text("""
        CREATE FUNCTION enforce_resource_lifecycle_timestamps()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                NEW.creation_time = statement_timestamp();
                NEW.modification_time = statement_timestamp();
                RETURN NEW;
            END IF;

            IF NEW.creation_time IS DISTINCT FROM OLD.creation_time THEN
                RAISE EXCEPTION 'resource creation_time is immutable';
            END IF;

            NEW.modification_time = statement_timestamp();
            RETURN NEW;
        END
        $$
    """))
    op.execute(sa.text("""
        CREATE TRIGGER enforce_resource_lifecycle_timestamps
        BEFORE INSERT OR UPDATE ON idtype
        FOR EACH ROW
        EXECUTE FUNCTION enforce_resource_lifecycle_timestamps()
    """))


def downgrade() -> None:
    op.execute(sa.text("DROP TRIGGER IF EXISTS enforce_resource_lifecycle_timestamps ON idtype"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS enforce_resource_lifecycle_timestamps()"))

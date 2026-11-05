"""Name instrument owners, upload session owners and postprocessing requesters by user.id.
Instrument owners that match no user stay empty, to be assigned by hand."""
import sqlalchemy as sa
from alembic import op

revision = "f3a9d1c7b842"
down_revision = "e61b3c9d4f05"
branch_labels = None
depends_on = None

COLUMNS = (
    ("instrument", "owner", "owner_user_id", "INSERT OR UPDATE", None),
    ("upload_sessions", "owner_user_id", "user_id", "INSERT", None),
    ("postprocessingrun", "requested_by", "requested_by_user_id", "INSERT", "SET NULL"),
)


def upgrade():
    op.alter_column("instrument", "owner", existing_type=sa.String(), nullable=True)
    for table, text_column, id_column, _, ondelete in COLUMNS:
        op.add_column(table, sa.Column(id_column, sa.Integer(), sa.ForeignKey("user.id", ondelete=ondelete), nullable=True))
        op.create_index(f"ix_{table}_{id_column}", table, [id_column])
        op.execute(sa.text(f'UPDATE {table} SET {id_column} = u.id FROM "user" u WHERE u.orcid = {table}.{text_column}'))
    # Pods from before this revision write only the text columns during the rollout; an ownership
    # transfer updates the instrument owner. Removed with the text columns.
    for table, text_column, id_column, events, _ in COLUMNS:
        op.execute(sa.text(f"""
            CREATE FUNCTION fill_{table}_{id_column}()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                IF TG_OP = 'INSERT' AND NEW.{id_column} IS NULL
                   OR TG_OP = 'UPDATE' AND NEW.{text_column} IS DISTINCT FROM OLD.{text_column} AND NEW.{id_column} IS NOT DISTINCT FROM OLD.{id_column} THEN
                    NEW.{id_column} := (SELECT id FROM "user" WHERE orcid = NEW.{text_column});
                END IF;
                RETURN NEW;
            END
            $$
        """))
        op.execute(sa.text(f"CREATE TRIGGER fill_{id_column} BEFORE {events} ON {table} FOR EACH ROW EXECUTE FUNCTION fill_{table}_{id_column}()"))


def downgrade():
    for table, _, id_column, _, _ in COLUMNS:
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS fill_{id_column} ON {table}"))
        op.execute(sa.text(f"DROP FUNCTION IF EXISTS fill_{table}_{id_column}()"))
        op.drop_index(f"ix_{table}_{id_column}", table_name=table)
        op.drop_column(table, id_column)
    op.execute(sa.text("UPDATE instrument SET owner = '' WHERE owner IS NULL"))
    op.alter_column("instrument", "owner", existing_type=sa.String(), nullable=False)

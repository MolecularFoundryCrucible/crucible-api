"""Name the people on deletion requests and the deletion audit log by user.id. The audit log keeps its text as history."""
import sqlalchemy as sa
from alembic import op

revision = "e61b3c9d4f05"
down_revision = "d2c8f4a7e913"
branch_labels = None
depends_on = None

COLUMNS = (
    ("deletionrequest", "requester_id", "requester_user_id"),
    ("deletionrequest", "reviewer_id", "reviewer_user_id"),
    ("deletionauditlog", "requester_id", "requester_user_id"),
    ("deletionauditlog", "reviewer_id", "reviewer_user_id"),
)


def upgrade():
    for table, text_column, id_column in COLUMNS:
        op.add_column(table, sa.Column(id_column, sa.Integer(), sa.ForeignKey("user.id"), nullable=True))
        op.create_index(f"ix_{table}_{id_column}", table, [id_column])
        op.execute(sa.text(f'UPDATE {table} SET {id_column} = u.id FROM "user" u WHERE u.orcid = {table}.{text_column}'))
    # Pods from before this revision write only the text columns during the rollout; a review sets
    # the reviewer by update. Audit rows are never updated. Removed with the text columns.
    for table, text_column, id_column in COLUMNS:
        events = "INSERT OR UPDATE" if table == "deletionrequest" else "INSERT"
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
    for table, _, id_column in COLUMNS:
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS fill_{id_column} ON {table}"))
        op.execute(sa.text(f"DROP FUNCTION IF EXISTS fill_{table}_{id_column}()"))
        op.drop_index(f"ix_{table}_{id_column}", table_name=table)
        op.drop_column(table, id_column)

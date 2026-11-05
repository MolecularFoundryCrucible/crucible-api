"""Name the person on API keys, Hub token families and Hub login codes by user.id."""
import sqlalchemy as sa
from alembic import op

revision = "7b4e9d2a1c60"
down_revision = "c5f18d2e6a37"
branch_labels = None
depends_on = None

TABLES = ("apitoken", "hubtokenfamily", "hublogincode")


def upgrade():
    for table in TABLES:
        op.add_column(table, sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=True))
        op.create_index(f"ix_{table}_user_id", table, ["user_id"])
        op.execute(sa.text(f'UPDATE {table} SET user_id = u.id FROM "user" u WHERE u.orcid = {table}.user_unique_id AND {table}.user_id IS NULL'))
    # Pods from before this revision write only user_unique_id during the rollout. Removed with that column.
    op.execute(sa.text("""
        CREATE FUNCTION fill_user_id_from_unique_id()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.user_id IS NULL AND NEW.user_unique_id IS NOT NULL THEN
                SELECT id INTO NEW.user_id FROM "user" WHERE orcid = NEW.user_unique_id;
            END IF;
            RETURN NEW;
        END
        $$
    """))
    for table in TABLES:
        op.execute(sa.text(f"CREATE TRIGGER fill_user_id BEFORE INSERT ON {table} FOR EACH ROW EXECUTE FUNCTION fill_user_id_from_unique_id()"))


def downgrade():
    for table in TABLES:
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS fill_user_id ON {table}"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS fill_user_id_from_unique_id()"))
    for table in TABLES:
        op.drop_index(f"ix_{table}_user_id", table_name=table)
        op.drop_column(table, "user_id")

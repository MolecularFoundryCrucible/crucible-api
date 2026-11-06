"""Revoke a key's hashed copy whenever its plain row is deleted, by any writer."""
import sqlalchemy as sa
from alembic import op

revision = "2b0ab3a4520a"
down_revision = "c9e1a4b7d362"
branch_labels = None
depends_on = None


def upgrade():
    # Pods from before the hashed table, rollbacks and operators revoke keys by deleting apitoken
    # rows; without this the hashed copy would keep working. Removed with the apitoken table.
    op.execute(sa.text("""
        CREATE FUNCTION revoke_hashed_api_key()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            UPDATE api_key SET revoked_at = now()
            WHERE key_hash = encode(sha256(convert_to(OLD.token, 'UTF8')), 'hex') AND revoked_at IS NULL;
            RETURN OLD;
        END
        $$
    """))
    op.execute(sa.text("CREATE TRIGGER revoke_hashed_api_key AFTER DELETE ON apitoken FOR EACH ROW EXECUTE FUNCTION revoke_hashed_api_key()"))


def downgrade():
    op.execute(sa.text("DROP TRIGGER IF EXISTS revoke_hashed_api_key ON apitoken"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS revoke_hashed_api_key()"))

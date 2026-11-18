"""Copy any API key still held only in plain text into api_key, then drop the plain apitoken table."""
import hashlib
from datetime import date, datetime, time, timedelta, timezone

import sqlalchemy as sa
from alembic import op

revision = "b0e044b67c87"
down_revision = "119e02fa5b77"
branch_labels = None
depends_on = None

KEY_LIFETIME = timedelta(days=365)


def _created(value):
    if not value:
        return None
    try:
        if len(value) == 10:
            return datetime.combine(date.fromisoformat(value), time.min, tzinfo=timezone.utc)
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def upgrade():
    connection = op.get_bind()
    known = {digest for (digest,) in connection.execute(sa.text("SELECT key_hash FROM api_key"))}
    for user_id, token, token_created, last_used_at in connection.execute(sa.text("SELECT user_id, token, token_created, last_used_at FROM apitoken")).all():
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        created = _created(token_created)
        if user_id is None or created is None or digest in known:
            continue
        connection.execute(sa.text(
            "INSERT INTO api_key (user_id, key_hash, key_last4, name, created_at, expires_at, last_used_at) "
            "VALUES (:user_id, :key_hash, :last4, 'Default key', :created, :expires, :last_used)"
        ), {"user_id": user_id, "key_hash": digest, "last4": token[-4:], "created": created, "expires": created + KEY_LIFETIME, "last_used": last_used_at})
        known.add(digest)
    op.drop_table("apitoken")
    op.execute(sa.text("DROP FUNCTION IF EXISTS revoke_hashed_api_key()"))


def downgrade():
    op.create_table(
        "apitoken",
        sa.Column("access_group_id", sa.String(), nullable=False),
        sa.Column("token", sa.String(), nullable=False),
        sa.Column("token_created", sa.String(), nullable=False),
        sa.Column("user_unique_id", sa.String(), sa.ForeignKey("user.orcid", name="fk_apitoken_user_orcid"), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=True),
        sa.PrimaryKeyConstraint("access_group_id", "token", name="apitoken_pkey"),
    )
    for column in ("access_group_id", "token", "user_id"):
        op.create_index(f"ix_apitoken_{column}", "apitoken", [column])
    op.execute(sa.text("CREATE TRIGGER fill_user_id BEFORE INSERT ON apitoken FOR EACH ROW EXECUTE FUNCTION fill_user_id_from_unique_id()"))
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

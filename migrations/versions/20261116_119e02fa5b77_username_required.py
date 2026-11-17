"""Give every account without a username one derived from its name, then require a username."""
import sqlalchemy as sa
from alembic import op

from app.utils.user_utils import _USERNAME_MAX, _username_collision_key, is_restricted_username, sanitize_username_from_name

revision = "119e02fa5b77"
down_revision = "9d4a2c7e6f13"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    taken = {_username_collision_key(name) for (name,) in connection.execute(sa.text('SELECT username FROM "user" WHERE username IS NOT NULL'))}
    missing = connection.execute(sa.text('SELECT id, first_name, last_name FROM "user" WHERE username IS NULL ORDER BY id')).all()
    for user_id, first_name, last_name in missing:
        base = sanitize_username_from_name(first_name or "", last_name or "")
        if len(base) < 3:
            base = "user"
        candidate, n = base, 2
        while is_restricted_username(candidate) or _username_collision_key(candidate) in taken:
            suffix = f"_{n}"
            candidate, n = f"{base[:_USERNAME_MAX - len(suffix)]}{suffix}", n + 1
        taken.add(_username_collision_key(candidate))
        connection.execute(sa.text('UPDATE "user" SET username = :username WHERE id = :id'), {"username": candidate, "id": user_id})
    op.alter_column("user", "username", existing_type=sa.String(length=32), nullable=False)


def downgrade():
    op.alter_column("user", "username", existing_type=sa.String(length=32), nullable=True)

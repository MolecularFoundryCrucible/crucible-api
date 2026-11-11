"""Enforce case-insensitive uniqueness of non-null user emails."""
from alembic import op
import sqlalchemy as sa

revision = "e5a1c8d93f27"
down_revision = "d94e1f2a7b05"
branch_labels = None
depends_on = None


def upgrade():
    duplicate = op.get_bind().execute(sa.text("""
        SELECT 1 FROM "user"
        WHERE email IS NOT NULL
        GROUP BY lower(email)
        HAVING count(*) > 1
        LIMIT 1
    """)).first()
    if duplicate is not None:
        raise RuntimeError(
            "user.email contains addresses shared by more than one user. "
            "Resolve the duplicates, then run the upgrade again."
        )
    with op.get_context().autocommit_block():
        op.create_index(
            "ux_user_email_lower",
            "user",
            [sa.text("lower(email)")],
            unique=True,
            postgresql_where=sa.text("email IS NOT NULL"),
            postgresql_concurrently=True,
        )


def downgrade():
    with op.get_context().autocommit_block():
        op.drop_index("ux_user_email_lower", table_name="user", if_exists=True, postgresql_concurrently=True)

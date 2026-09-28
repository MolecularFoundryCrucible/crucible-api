"""index Hub authentication expiry

Revision ID: e92a6b1c730f
Revises: d81c3a4f62b0
Create Date: 2026-09-28 00:00:00.000000

"""
from alembic import op


revision = "e92a6b1c730f"
down_revision = "d81c3a4f62b0"
branch_labels = None
depends_on = None

INDEXES = (
    ("hublogintransaction", "state_hash"),
    ("hublogincode", "code_hash"),
    ("hubaccesstoken", "token_hash"),
    ("hubtokenfamily", "id"),
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for table, key in INDEXES:
            op.create_index(
                f"ix_{table}_expiry", table, ["expires_at", key],
                postgresql_concurrently=True,
            )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for table, _ in reversed(INDEXES):
            op.drop_index(f"ix_{table}_expiry", table_name=table, postgresql_concurrently=True)

"""Index dataset and sample owner_orcid for owner filters and facets."""
from alembic import op

revision = "5be2f0c8d4a1"
down_revision = "c3f1d29a7b64"
branch_labels = None
depends_on = None

TABLES = ("dataset", "sample")


def upgrade():
    with op.get_context().autocommit_block():
        for table in TABLES:
            op.create_index(f"ix_{table}_owner_orcid", table, ["owner_orcid"], postgresql_concurrently=True, if_not_exists=True)


def downgrade():
    with op.get_context().autocommit_block():
        for table in reversed(TABLES):
            op.drop_index(f"ix_{table}_owner_orcid", table_name=table, postgresql_concurrently=True, if_exists=True)

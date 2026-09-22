"""add resource embedding

Revision ID: 9e4c2a7b6d31
Revises: 5c8e1a4d7b20
Create Date: 2026-09-22 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
from pgvector.sqlalchemy import Vector
import sqlalchemy as sa


revision: str = "9e4c2a7b6d31"
down_revision: Union[str, Sequence[str], None] = "5c8e1a4d7b20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # PostgreSQL must know the vector type before the table can be created.
    # IF NOT EXISTS preserves installations that already use pgvector.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "resource_embedding",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("resource_mfid", sa.String(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("chunk_text", sa.String(), nullable=False),
        sa.Column("embedding", Vector(768), nullable=True),
        sa.Column("model_name", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["resource_mfid"], ["idtype.mfid"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_resource_embedding_resource_mfid",
        "resource_embedding",
        ["resource_mfid"],
        unique=False,
    )
    op.create_index(
        "ix_resource_embedding_embedding_cosine",
        "resource_embedding",
        ["embedding"],
        postgresql_using="ivfflat",
        postgresql_ops={"embedding": "vector_cosine_ops"},
        postgresql_with={"lists": 100},
    )


def downgrade() -> None:
    op.drop_index(
        "ix_resource_embedding_embedding_cosine",
        table_name="resource_embedding",
        postgresql_using="ivfflat",
    )
    op.drop_index(
        "ix_resource_embedding_resource_mfid",
        table_name="resource_embedding",
    )
    op.drop_table("resource_embedding")
    # Do not drop the vector extension: other database objects may use it.

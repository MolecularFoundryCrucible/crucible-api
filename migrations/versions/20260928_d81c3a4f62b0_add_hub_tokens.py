"""add Hub token families and credentials

Revision ID: d81c3a4f62b0
Revises: 4a7d2c9e1f60
Create Date: 2026-09-28 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d81c3a4f62b0"
down_revision: Union[str, Sequence[str], None] = "4a7d2c9e1f60"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "hubtokenfamily",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_unique_id", sa.String(), sa.ForeignKey("user.orcid"), nullable=False),
        sa.Column("client_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_refreshed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_hubtokenfamily_user_unique_id", "hubtokenfamily", ["user_unique_id"])
    op.create_index("ix_hubtokenfamily_client_id", "hubtokenfamily", ["client_id"])
    op.create_table(
        "hubrefreshtoken",
        sa.Column("token_hash", sa.String(length=64), primary_key=True),
        sa.Column("family_id", sa.String(length=36), sa.ForeignKey("hubtokenfamily.id"), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("spent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_hubrefreshtoken_family_id", "hubrefreshtoken", ["family_id"])
    op.create_table(
        "hubaccesstoken",
        sa.Column("token_hash", sa.String(length=64), primary_key=True),
        sa.Column("family_id", sa.String(length=36), sa.ForeignKey("hubtokenfamily.id"), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_hubaccesstoken_family_id", "hubaccesstoken", ["family_id"])
    op.create_table(
        "hublogintransaction",
        sa.Column("state_hash", sa.String(length=64), primary_key=True),
        sa.Column("browser_secret_hash", sa.String(length=64), nullable=False),
        sa.Column("client_id", sa.String(), nullable=False),
        sa.Column("callback_uri", sa.String(), nullable=False),
        sa.Column("hub_state", sa.String(), nullable=False),
        sa.Column("code_challenge", sa.String(length=43), nullable=False),
        sa.Column("nonce", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "hublogincode",
        sa.Column("code_hash", sa.String(length=64), primary_key=True),
        sa.Column("user_unique_id", sa.String(), sa.ForeignKey("user.orcid"), nullable=False),
        sa.Column("client_id", sa.String(), nullable=False),
        sa.Column("callback_uri", sa.String(), nullable=False),
        sa.Column("code_challenge", sa.String(length=43), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_hublogincode_user_unique_id", "hublogincode", ["user_unique_id"])


def downgrade() -> None:
    op.drop_index("ix_hublogincode_user_unique_id", table_name="hublogincode")
    op.drop_table("hublogincode")
    op.drop_table("hublogintransaction")
    op.drop_index("ix_hubaccesstoken_family_id", table_name="hubaccesstoken")
    op.drop_table("hubaccesstoken")
    op.drop_index("ix_hubrefreshtoken_family_id", table_name="hubrefreshtoken")
    op.drop_table("hubrefreshtoken")
    op.drop_index("ix_hubtokenfamily_client_id", table_name="hubtokenfamily")
    op.drop_index("ix_hubtokenfamily_user_unique_id", table_name="hubtokenfamily")
    op.drop_table("hubtokenfamily")

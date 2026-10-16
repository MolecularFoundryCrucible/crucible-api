"""Add postprocessing runs."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "aa8eb05c643d"
down_revision = "b644cf2e7240"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "postprocessingrun",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dataset_mfid", sa.String(), sa.ForeignKey("dataset.unique_id", ondelete="CASCADE"), nullable=False),
        sa.Column("pipeline", sa.String(), nullable=False),
        sa.Column("parameters", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("status", sa.String(), nullable=False, server_default="requested"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("requested_by", sa.String(), sa.ForeignKey("user.orcid", ondelete="SET NULL"), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('requested', 'running', 'completed', 'failed')", name="ck_postprocessingrun_status"),
    )
    op.create_index("ix_postprocessingrun_dataset_requested", "postprocessingrun", ["dataset_mfid", "requested_at"])


def downgrade():
    op.drop_index("ix_postprocessingrun_dataset_requested", table_name="postprocessingrun")
    op.drop_table("postprocessingrun")

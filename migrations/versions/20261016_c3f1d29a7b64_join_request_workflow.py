"""Join request workflow: timestamps, status and length checks, granted role, review index."""
from alembic import op
import sqlalchemy as sa

revision = "c3f1d29a7b64"
down_revision = "aa8eb05c643d"
branch_labels = None
depends_on = None

ROLE = sa.Enum("viewer", "contributor", "editor", "admin", "owner", name="resource_role", create_type=False)


def upgrade():
    op.execute("UPDATE accessgroupjoinrequest SET request_time = now()::text WHERE request_time IS NULL")
    op.alter_column("accessgroupjoinrequest", "request_time", type_=sa.DateTime(timezone=True),
                    postgresql_using="request_time::timestamptz", nullable=False, server_default=sa.text("now()"))
    op.alter_column("accessgroupjoinrequest", "review_time", type_=sa.DateTime(timezone=True),
                    postgresql_using="review_time::timestamptz")
    op.add_column("accessgroupjoinrequest", sa.Column("granted_role", ROLE, nullable=True))
    op.create_check_constraint("ck_agjr_status", "accessgroupjoinrequest", "status IN ('pending', 'approved', 'rejected', 'withdrawn')")
    op.create_check_constraint("ck_agjr_reason_length", "accessgroupjoinrequest", "length(reason) <= 2000")
    op.create_check_constraint("ck_agjr_reviewer_notes_length", "accessgroupjoinrequest", "length(reviewer_notes) <= 2000")
    op.create_index("ix_agjr_group_status_time", "accessgroupjoinrequest", ["accessgroup_id", "status", "request_time"])


def downgrade():
    op.drop_index("ix_agjr_group_status_time", table_name="accessgroupjoinrequest")
    op.drop_constraint("ck_agjr_reviewer_notes_length", "accessgroupjoinrequest", type_="check")
    op.drop_constraint("ck_agjr_reason_length", "accessgroupjoinrequest", type_="check")
    op.drop_constraint("ck_agjr_status", "accessgroupjoinrequest", type_="check")
    op.drop_column("accessgroupjoinrequest", "granted_role")
    op.alter_column("accessgroupjoinrequest", "review_time", type_=sa.Text(), postgresql_using="review_time::text")
    op.alter_column("accessgroupjoinrequest", "request_time", type_=sa.Text(), postgresql_using="request_time::text",
                    nullable=True, server_default=None)

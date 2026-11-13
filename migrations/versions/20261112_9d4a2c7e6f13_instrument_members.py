"""Move instrument maintainers into a members group per instrument and let members read the instrument's datasets.
The members group holds the instrument record's owner grant and viewer on every dataset assigned to the instrument;
operator group grants are made to match dataset assignment."""
import sqlalchemy as sa
from alembic import op

revision = "9d4a2c7e6f13"
down_revision = "3b8e5d1f7c24"
branch_labels = None
depends_on = None

MEMBERS = "i.unique_id || :suffix"


def _run(sql):
    op.execute(sa.text(sql).bindparams(suffix=":members") if ":suffix" in sql else sa.text(sql))


def upgrade():
    _run(f"""
        INSERT INTO accessgroup (group_name, group_type)
        SELECT {MEMBERS}, 'instrument_member' FROM instrument i
        WHERE NOT EXISTS (SELECT 1 FROM accessgroup g WHERE g.group_name = {MEMBERS})
    """)
    _run(f"""
        INSERT INTO useraccessgrouplink (user_id, accessgroup_id, role)
        SELECT person.user_id, members.id, acl.permission
        FROM resourceacl acl
        JOIN instrument i ON i.unique_id = acl.mfid
        JOIN accessgroup identity_group ON identity_group.id = acl.accessgroup_id AND identity_group.group_type = 'identity'
        JOIN useraccessgrouplink person ON person.accessgroup_id = identity_group.id AND person.role = 'owner'
        JOIN "user" u ON u.id = person.user_id AND NOT COALESCE(u.is_service_account, false)
        JOIN accessgroup members ON members.group_name = {MEMBERS} AND members.group_type = 'instrument_member'
        ON CONFLICT DO NOTHING
    """)
    _run("""
        DELETE FROM resourceacl acl
        USING instrument i, accessgroup g
        WHERE acl.mfid = i.unique_id AND g.id = acl.accessgroup_id AND g.group_type IN ('identity', 'project')
    """)
    _run(f"""
        INSERT INTO resourceacl (mfid, accessgroup_id, read, write, permission)
        SELECT i.unique_id, members.id, true, true, 'owner'
        FROM instrument i
        JOIN accessgroup members ON members.group_name = {MEMBERS} AND members.group_type = 'instrument_member'
        ON CONFLICT DO NOTHING
    """)
    _run("""
        INSERT INTO resourceacl (mfid, accessgroup_id, read, write, permission)
        SELECT d.unique_id, members.id, true, false, 'viewer'
        FROM dataset d
        JOIN accessgroup members ON members.group_name = d.instrument_mfid || :suffix AND members.group_type = 'instrument_member'
        ON CONFLICT DO NOTHING
    """)
    _run("""
        DELETE FROM resourceacl acl
        USING accessgroup operator_group
        WHERE operator_group.id = acl.accessgroup_id AND operator_group.group_type = 'instrument'
          AND NOT EXISTS (SELECT 1 FROM dataset d WHERE d.unique_id = acl.mfid AND d.instrument_mfid = operator_group.group_name)
    """)
    _run("""
        INSERT INTO resourceacl (mfid, accessgroup_id, read, write, permission)
        SELECT d.unique_id, operator_group.id, true, true, 'contributor'
        FROM dataset d
        JOIN accessgroup operator_group ON operator_group.group_name = d.instrument_mfid AND operator_group.group_type = 'instrument'
        ON CONFLICT DO NOTHING
    """)


def downgrade():
    _run("""
        DELETE FROM resourceacl acl USING accessgroup g
        WHERE g.id = acl.accessgroup_id AND g.group_type = 'instrument_member'
    """)
    _run("""
        INSERT INTO resourceacl (mfid, accessgroup_id, read, write, permission)
        SELECT left(members.group_name, length(members.group_name) - length(:suffix)), identity_group.id,
               true, member.role IN ('editor', 'admin', 'owner'), member.role
        FROM useraccessgrouplink member
        JOIN accessgroup members ON members.id = member.accessgroup_id AND members.group_type = 'instrument_member'
        JOIN useraccessgrouplink self_link ON self_link.user_id = member.user_id AND self_link.role = 'owner'
        JOIN accessgroup identity_group ON identity_group.id = self_link.accessgroup_id AND identity_group.group_type = 'identity'
        ON CONFLICT DO NOTHING
    """)
    _run("""
        DELETE FROM useraccessgrouplink link USING accessgroup g
        WHERE g.id = link.accessgroup_id AND g.group_type = 'instrument_member'
    """)
    _run("DELETE FROM accessgroup WHERE group_type = 'instrument_member'")

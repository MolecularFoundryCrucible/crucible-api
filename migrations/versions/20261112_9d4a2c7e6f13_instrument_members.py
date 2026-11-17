"""Move instrument maintainers into a members group per instrument and let members read the instrument's datasets.
People with a grant on the instrument, directly or through a project, become members at the highest role they held
(contributor becomes editor); an instrument whose owner was a service account passes to its highest-ranking member.
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
        SELECT held.user_id, members.id, max(held.role)
        FROM (
            SELECT acl.mfid, person.user_id,
                   CASE acl.permission WHEN 'contributor' THEN 'editor' ELSE acl.permission END::resource_role AS role
            FROM resourceacl acl
            JOIN accessgroup identity_group ON identity_group.id = acl.accessgroup_id AND identity_group.group_type = 'identity'
            JOIN useraccessgrouplink person ON person.accessgroup_id = identity_group.id AND person.role = 'owner'
            UNION ALL
            SELECT acl.mfid, project_member.user_id,
                   CASE least(project_member.role, acl.permission) WHEN 'contributor' THEN 'editor' WHEN 'owner' THEN 'admin'
                        ELSE least(project_member.role, acl.permission) END::resource_role
            FROM resourceacl acl
            JOIN accessgroup project_group ON project_group.id = acl.accessgroup_id AND project_group.group_type = 'project'
            JOIN useraccessgrouplink project_member ON project_member.accessgroup_id = project_group.id
        ) held
        JOIN instrument i ON i.unique_id = held.mfid
        JOIN "user" u ON u.id = held.user_id AND NOT COALESCE(u.is_service_account, false)
        JOIN accessgroup members ON members.group_name = {MEMBERS} AND members.group_type = 'instrument_member'
        GROUP BY held.user_id, members.id
        ON CONFLICT DO NOTHING
    """)
    _run("""
        UPDATE useraccessgrouplink link SET role = 'owner'
        FROM (
            SELECT DISTINCT ON (candidate.accessgroup_id) candidate.accessgroup_id, candidate.user_id
            FROM useraccessgrouplink candidate
            JOIN accessgroup g ON g.id = candidate.accessgroup_id AND g.group_type = 'instrument_member'
            WHERE NOT EXISTS (SELECT 1 FROM useraccessgrouplink held WHERE held.accessgroup_id = candidate.accessgroup_id AND held.role = 'owner')
            ORDER BY candidate.accessgroup_id, candidate.role DESC, candidate.user_id
        ) successor
        WHERE link.accessgroup_id = successor.accessgroup_id AND link.user_id = successor.user_id
    """)
    _run(f"""
        UPDATE instrument i SET owner_user_id = u.id, owner = u.orcid
        FROM accessgroup members
        JOIN useraccessgrouplink link ON link.accessgroup_id = members.id AND link.role = 'owner'
        JOIN "user" u ON u.id = link.user_id
        WHERE members.group_name = {MEMBERS} AND members.group_type = 'instrument_member' AND i.owner_user_id IS DISTINCT FROM u.id
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
        JOIN idtype record ON record.mfid = d.unique_id
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
        JOIN idtype record ON record.mfid = d.unique_id
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

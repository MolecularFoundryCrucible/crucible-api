import re

from fastapi import HTTPException
from sqlalchemy import and_, func
from sqlmodel import select
from app.models.core_models import AccessGroup, Instrument, Project, User, UserAccessGroupLink, UserProjectLink
from app.utils.permissions import Role
from app.utils.sql_utils import add_or_update, touch_resource_modification_time
from app.utils.user_utils import sql_add_user_to_access_group

# Deliberately looser than _USERNAME_RE: 6,707 of 6,757 existing project IDs
# contain uppercase (the MFP* proposal IDs) and 3 begin with a digit, so
# requiring lowercase or a leading letter would reject almost every real
# project. Only 4 existing rows fail this, all of them junk.
_PROJECT_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{1,63}$')

# Access-group names that are not derived from an entity. A project taking one
# of these would BE that group, because create_project reuses an existing group
# rather than failing.
RESERVED_PROJECT_IDS = frozenset({"admin", "read_only", "public"})


def validate_project_id(project_id: str) -> str:
    """Check a project_id is well-formed and not a reserved group name.

    create_project reuses an existing access group of the same name, so
    project_id='admin' would add the lead to the admin group. Case-insensitive.
    A guard only; the fix is MFID-named groups (see the plan).
    """
    if not isinstance(project_id, str) or not _PROJECT_ID_RE.match(project_id):
        raise ValueError(
            "project_id must be 2-64 chars, start with a letter or digit, "
            "and contain only letters, digits, underscores, hyphens"
        )
    if project_id.lower() in RESERVED_PROJECT_IDS:
        raise ValueError(
            f"project_id '{project_id}' is reserved "
            f"({', '.join(sorted(RESERVED_PROJECT_IDS))} name access groups, not projects)"
        )
    return project_id


def assert_project_id_available(session, project_id: str) -> None:
    """Raise 409 if project_id collides with an existing access group or
    instrument name. Case-insensitive.

    Call after the duplicate-project check: the project does not exist by then,
    so any group holding the name belongs to something else — an ORCID group,
    an instrument, a reserved group, an orphan. Instrument.instrument_name is
    checked directly, not just AccessGroup, since an instrument with no
    dataset filed against it yet has no group row.
    """
    existing_group = session.exec(
        select(AccessGroup).where(func.lower(AccessGroup.group_name) == project_id.lower())
    ).one_or_none()
    if existing_group is not None:
        raise HTTPException(
            status_code=409,
            detail=(f"project_id '{project_id}' collides with an existing access group. "
                    f"Access group names are shared with users, instruments and "
                    f"reserved groups; choose a different project_id."),
        )
    existing_instrument = session.exec(
        select(Instrument).where(func.lower(Instrument.instrument_name) == project_id.lower())
    ).one_or_none()
    if existing_instrument is not None:
        raise HTTPException(
            status_code=409,
            detail=f"project_id '{project_id}' collides with an existing instrument name.",
        )


def _project_group_name(proj_id: str, session) -> "str | None":
    """Resolve proj_id to its access group's name — the project's MFID
    (Project.unique_id), not project_id itself. None if proj_id does not name
    a real project, or that project has no unique_id yet (pre-backfill).

    CUTOVER DEPENDENCY: every function below assumes every real project has
    a populated unique_id and its access group is already named after it.
    Both must be true in the target database before this code is deployed —
    see migrations/data/20260810_backfill_project_unique_id.py and the (not
    yet written) group-rename backfill. Until then this silently returns None
    for every project, which reads as "not a member of anything."
    """
    project = session.exec(select(Project).where(Project.project_id == proj_id)).one_or_none()
    return project.unique_id if project else None


def sql_add_user_to_project(user: User, proj_id: str, session, role=None):
    """Add a user to a project's membership and access group. Idempotent.

    The UserProjectLink write is legacy: UserAccessGroupLink is what
    authorization and every membership read actually consult (see
    is_project_member / sql_get_projects_for_user). Kept in step here only so
    existing readers of UserProjectLink are not broken before its phase-5
    drop — see docs/permission_system_plan.md.
    """
    group_name = _project_group_name(proj_id, session)
    if group_name is None:
        raise HTTPException(status_code=404, detail=f"Project '{proj_id}' not found or not yet provisioned")
    add_or_update(session,
                  UserProjectLink,
                  where=(and_(UserProjectLink.user_id == user.id, UserProjectLink.project_id == proj_id)),
                  user_id=user.id,
                  project_id=proj_id)
    sql_add_user_to_access_group(user.unique_id, group_name, session, role=role)
    touch_resource_modification_time(session, proj_id)


def is_project_group(group_name: str, session) -> bool:
    """Whether this group name is an existing project's own access group —
    named after the project's MFID (Project.unique_id), not its project_id.

    Used to keep the generic /users/{orcid}/access_groups/{group_name} routes
    from being a side door around /projects/{proj_id}/users/{orcid}, which
    knows the project's own authorization bar (editor+, capped at your own
    role) — the generic route only checks platform-admin.
    """
    return session.exec(select(Project).where(Project.unique_id == group_name)).first() is not None


def is_project_member(user_id: int, proj_id: str, session) -> bool:
    """Whether the user has any standing in the project's own access group —
    the authoritative definition of project membership. Not UserProjectLink:
    that table can drift from real access (e.g. via the generic
    POST/DELETE /users/{orcid}/access_groups/{group_name} routes, which never
    touch it), so anything gating on "is a member" must ask the table
    authorization itself reads.
    """
    group_name = _project_group_name(proj_id, session)
    if group_name is None:
        return False
    return session.exec(
        select(UserAccessGroupLink)
        .join(AccessGroup, AccessGroup.id == UserAccessGroupLink.accessgroup_id)
        .where(AccessGroup.group_name == group_name, UserAccessGroupLink.user_id == user_id)
    ).first() is not None


def is_project_owner(user_unique_id: str, proj_id: str, session) -> bool:
    """Whether the user holds `owner` standing in proj_id's own access group —
    the real definition of "project lead" (see docs/permission_system_plan.md,
    "Project lead becomes a role"). `Project.project_lead_orcid` is a
    denormalized display pointer kept in sync by transfer_ownership; this is
    what authorization actually reads.

    Confirms proj_id names a real project first — owner standing in an
    arbitrarily-named access group (e.g. the caller's own ORCID group, where
    everyone is always owner) would otherwise trivially pass for a
    non-project name.
    """
    group_name = _project_group_name(proj_id, session)
    if group_name is None:
        return False
    return session.exec(
        select(UserAccessGroupLink)
        .join(AccessGroup, AccessGroup.id == UserAccessGroupLink.accessgroup_id)
        .join(User, User.id == UserAccessGroupLink.user_id)
        .where(
            AccessGroup.group_name == group_name,
            User.unique_id == user_unique_id,
            UserAccessGroupLink.role == Role.OWNER.db_value,
        )
    ).first() is not None


def project_standing(orcid: str, proj_id: str, session) -> "Role | None":
    group_name = _project_group_name(proj_id, session)
    if group_name is None:
        return None
    row = session.exec(
        select(UserAccessGroupLink.role)
        .join(AccessGroup, AccessGroup.id == UserAccessGroupLink.accessgroup_id)
        .join(User, User.id == UserAccessGroupLink.user_id)
        .where(AccessGroup.group_name == group_name, User.unique_id == orcid)
    ).one_or_none()
    return Role.from_db(row) if row else None


def sql_get_projects_for_user(orcid, session, options=None):
    """Get all projects for a user by ORCID. Returns empty list if user not found.

    Reads membership from the project's own access group standing, the same
    source is_project_member and effective_role use — not UserProjectLink,
    which can drift from it (see is_project_member).
    """
    user = session.exec(select(User).where(User.unique_id == orcid)).one_or_none()
    if user is None:
        return None

    query = (
        select(Project)
        .join(AccessGroup, AccessGroup.group_name == Project.unique_id)
        .join(UserAccessGroupLink, and_(
            UserAccessGroupLink.accessgroup_id == AccessGroup.id,
            UserAccessGroupLink.user_id == user.id,
        ))
    )
    if options:
        query = query.options(*options)
    return session.exec(query).all()

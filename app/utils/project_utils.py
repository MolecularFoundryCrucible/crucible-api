import re
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import and_, func
from sqlmodel import select
from app.models.core_models import AccessGroup, Project, User, UserAccessGroupLink, UserProjectLink
from app.models.projects import ProjectReferenceRead
from app.general_utils import is_valid_mfid
from app.utils.permissions import AccessGroupType, Role
from app.utils.sql_utils import add_or_update, touch_resource_modification_time
from app.utils.user_utils import sql_add_user_to_access_group

# Project IDs retain uppercase and leading-digit support for existing proposal
# identifiers. The 25-character maximum keeps slugs distinct from MFIDs.
_PROJECT_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{2,24}$')

# Access-group names that are not derived from an entity. A project taking one
# of these would BE that group, because create_project reuses an existing group
# rather than failing.
RESERVED_PROJECT_IDS = frozenset({"admin", "read_only", "public"})


def project_id_matches(project_id: str):
    return func.lower(Project.project_id) == project_id.lower()


def validate_project_id(project_id: str) -> str:
    """Check a project_id is well-formed and not a reserved group name.

    create_project reuses an existing access group of the same name, so
    project_id='admin' would add the lead to the admin group. Case-insensitive.
    A guard only; the fix is MFID-named groups (see the plan).
    """
    if not isinstance(project_id, str) or not _PROJECT_ID_RE.fullmatch(project_id):
        raise ValueError(
            "project_id must be 3-25 chars, start with a letter or digit, "
            "and contain only letters, digits, underscores, hyphens"
        )
    if project_id.lower() in RESERVED_PROJECT_IDS:
        raise ValueError(
            f"project_id '{project_id}' is reserved "
            f"({', '.join(sorted(RESERVED_PROJECT_IDS))} name access groups, not projects)"
        )
    return project_id


def assert_project_id_available(session, project_id: str) -> None:
    """Raise 409 if project_id collides with an existing access group.
    Case-insensitive.

    Call after the duplicate-project check: the project does not exist by then,
    so any group holding the name belongs to something else — an ORCID group,
    a reserved group, an orphan.
    """
    existing_group = session.exec(
        select(AccessGroup).where(func.lower(AccessGroup.group_name) == project_id.lower())
    ).one_or_none()
    if existing_group is not None:
        raise HTTPException(
            status_code=409,
            detail=(f"project_id '{project_id}' collides with an existing access group. "
                    f"Access group names are shared with users and reserved groups; "
                    f"choose a different project_id."),
        )


def resolve_project_id_to_mfid(proj_id: str, session) -> "str | None":
    """Resolve a project_id label to its Project.unique_id (MFID). None if
    proj_id does not name a real project, or that project has no unique_id
    yet. Used both for access-group-name resolution and, since it does the
    same lookup, for repointing Dataset/Sample FKs onto the MFID.
    """
    project = session.exec(select(Project).where(project_id_matches(proj_id))).one_or_none()
    return project.unique_id if project else None


def project_not_found(project_id: Optional[str], project_mfid: Optional[str]) -> HTTPException:
    reference = project_mfid or project_id
    detail = f"Project '{reference}' not found"
    if project_mfid is None and project_id is not None and is_valid_mfid(project_id):
        detail += ". project_id takes the project slug; pass an MFID as project_mfid."
    return HTTPException(status_code=404, detail=detail)


def resolve_project_identifiers(
    session,
    project_id: Optional[str],
    project_mfid: Optional[str],
) -> Optional[Project]:
    if project_id is None and project_mfid is None:
        return None

    if project_mfid is not None:
        project = session.exec(
            select(Project).where(Project.unique_id == project_mfid)
        ).one_or_none()
    else:
        project = session.exec(
            select(Project).where(project_id_matches(project_id))
        ).one_or_none()

    if (
        project is not None
        and project_id is not None
        and project.project_id.casefold() != project_id.casefold()
    ):
        raise HTTPException(
            status_code=422,
            detail="project_id and project_mfid identify different projects",
        )
    return project


def resolve_project_filter_mfid(
    session,
    project_id: Optional[str],
    project_mfid: Optional[str],
) -> Optional[str]:
    if project_id is None:
        return project_mfid

    project = session.exec(
        select(Project).where(project_id_matches(project_id))
    ).one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    if project_mfid is not None and project.unique_id != project_mfid:
        raise HTTPException(
            status_code=422,
            detail="project_id and project_mfid identify different projects",
        )
    return project.unique_id


def resolve_project_reference(project_ref: str, session, *, options=()) -> "Project | None":
    if is_valid_mfid(project_ref):
        query = select(Project).where(Project.unique_id == project_ref)
    else:
        query = select(Project).where(project_id_matches(project_ref))
    if options:
        query = query.options(*options)
    return session.exec(query).one_or_none()


def resolve_project_mfids_to_ids(mfids, session) -> dict:
    """Batch mfid -> project_id, for response serialization. One query
    regardless of how many rows are being resolved — never loop-and-query
    per row."""
    mfid_set = {m for m in mfids if m}
    if not mfid_set:
        return {}
    rows = session.exec(
        select(Project.unique_id, Project.project_id).where(Project.unique_id.in_(mfid_set))
    ).all()
    return dict(rows)


def resolve_project_mfids_to_references(mfids, session) -> dict[str, ProjectReferenceRead]:
    mfid_set = {m for m in mfids if m}
    if not mfid_set:
        return {}
    rows = session.exec(
        select(Project.unique_id, Project.project_id, Project.title)
        .where(Project.unique_id.in_(mfid_set))
    ).all()
    return {
        unique_id: ProjectReferenceRead(
            unique_id=unique_id,
            project_id=project_id,
            title=title,
        )
        for unique_id, project_id, title in rows
    }


def sql_add_user_to_project(user: User, proj_id: str, session, role=None):
    """Add a user to a project's membership and access group. Idempotent.

    The UserProjectLink write is legacy: UserAccessGroupLink is what
    authorization and every membership read actually consult (see
    is_project_member / sql_get_projects_for_user). Kept in step here only so
    existing readers of UserProjectLink are not broken before its phase-5
    drop — see docs/permission_system_plan.md.
    """
    group_name = resolve_project_id_to_mfid(proj_id, session)
    if group_name is None:
        raise HTTPException(status_code=404, detail=f"Project '{proj_id}' not found or not yet provisioned")
    add_or_update(session,
                  UserProjectLink,
                  where=(and_(UserProjectLink.user_id == user.id, UserProjectLink.project_id == proj_id)),
                  user_id=user.id,
                  project_id=proj_id)
    sql_add_user_to_access_group(
        user.unique_id,
        group_name,
        session,
        role=role,
        group_type=AccessGroupType.PROJECT,
    )
    touch_resource_modification_time(session, group_name)


def is_project_group(group_name: str, session) -> bool:
    """Whether this group name is an existing project's own access group —
    named after the project's MFID (Project.unique_id), not its project_id.

    Used to keep the generic /users/{orcid}/access_groups/{group_name} routes
    from being a side door around /projects/{proj_id}/users/{orcid}, which
    knows the project's own authorization bar (editor+, capped at your own
    role) — the generic route only checks platform-admin.
    """
    return session.exec(
        select(AccessGroup).where(
            AccessGroup.group_name == group_name,
            AccessGroup.group_type == AccessGroupType.PROJECT.value,
        )
    ).first() is not None


def is_project_member(user_id: int, proj_id: str, session) -> bool:
    """Whether the user has any standing in the project's own access group —
    the authoritative definition of project membership. Not UserProjectLink:
    that table can drift from real access (e.g. via the generic
    POST/DELETE /users/{orcid}/access_groups/{group_name} routes, which never
    touch it), so anything gating on "is a member" must ask the table
    authorization itself reads.
    """
    group_name = resolve_project_id_to_mfid(proj_id, session)
    if group_name is None:
        return False
    return session.exec(
        select(UserAccessGroupLink)
        .join(AccessGroup, AccessGroup.id == UserAccessGroupLink.accessgroup_id)
        .where(
            AccessGroup.group_name == group_name,
            AccessGroup.group_type == AccessGroupType.PROJECT.value,
            UserAccessGroupLink.user_id == user_id,
        )
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
    group_name = resolve_project_id_to_mfid(proj_id, session)
    if group_name is None:
        return False
    return session.exec(
        select(UserAccessGroupLink)
        .join(AccessGroup, AccessGroup.id == UserAccessGroupLink.accessgroup_id)
        .join(User, User.id == UserAccessGroupLink.user_id)
        .where(
            AccessGroup.group_name == group_name,
            AccessGroup.group_type == AccessGroupType.PROJECT.value,
            User.unique_id == user_unique_id,
            UserAccessGroupLink.role == Role.OWNER.db_value,
        )
    ).first() is not None


def project_standing_by_mfid(orcid: str, project_mfid: str, session) -> "Role | None":
    row = session.exec(
        select(UserAccessGroupLink.role)
        .join(AccessGroup, AccessGroup.id == UserAccessGroupLink.accessgroup_id)
        .join(User, User.id == UserAccessGroupLink.user_id)
        .where(
            AccessGroup.group_name == project_mfid,
            AccessGroup.group_type == AccessGroupType.PROJECT.value,
            User.unique_id == orcid,
        )
    ).one_or_none()
    return Role.from_db(row) if row else None


def project_standing(orcid: str, proj_id: str, session) -> "Role | None":
    project_mfid = resolve_project_id_to_mfid(proj_id, session)
    if project_mfid is None:
        return None
    return project_standing_by_mfid(orcid, project_mfid, session)


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
        .where(AccessGroup.group_type == AccessGroupType.PROJECT.value)
    )
    if options:
        query = query.options(*options)
    return session.exec(query).all()

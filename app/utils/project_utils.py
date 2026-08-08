import re

from fastapi import HTTPException
from sqlalchemy import and_, join
from sqlmodel import select
from app.models.core_models import AccessGroup, Project, User, UserProjectLink
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
    """Check a project_id is well-formed and not a reserved name.

    The reserved check matters more than it looks. `create_project` derives the
    project's access group from this value via add_or_update, which **reuses**
    an existing group instead of rejecting the name. So a project called
    `admin` attaches itself to the admin access group and appends its lead to
    it — a one-request path from any authenticated user to platform
    administrator. `public` is the mirror image: the project's resources become
    readable by every principal, since the public group is injected into all of
    them.

    Case-insensitive, because `PUBLIC` creating a group distinct from `public`
    is confusing even where it is not exploitable.

    This is a guard, not the fix. The real fix is naming groups after MFIDs so
    a caller-supplied string can never land in the group namespace at all —
    see docs/permission_system_plan.md.
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
    """Raise 409 if this project_id would collide with an existing access group.

    Call after the duplicate-project check. At that point the project itself
    does not exist, so any access group already holding this name belongs to
    something else — a user's ORCID group, an instrument, a reserved group, or
    a typo-created orphan. Reusing it would silently merge two unrelated
    things.

    One check covers every case, which is why it is a group lookup rather than
    a list of entity types to exclude.
    """
    existing = session.exec(
        select(AccessGroup).where(AccessGroup.group_name == project_id)
    ).one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=(f"project_id '{project_id}' collides with an existing access group. "
                    f"Access group names are shared with users, instruments and "
                    f"reserved groups; choose a different project_id."),
        )


def sql_add_user_to_project(user: User, proj_id: str, session):
    """Add a user to a project's membership and access group. Idempotent."""
    add_or_update(session,
                  UserProjectLink,
                  where=(and_(UserProjectLink.user_id == user.id, UserProjectLink.project_id == proj_id)),
                  user_id=user.id,
                  project_id=proj_id)
    sql_add_user_to_access_group(user.unique_id, proj_id, session)
    touch_resource_modification_time(session, proj_id)


def sql_get_projects_for_user(orcid, session, options=None):
    """Get all projects for a user by ORCID. Returns empty list if user not found."""
    user = session.exec(select(User).where(User.unique_id == orcid)).one_or_none()
    if user is None:
        return None

    query = (
        select(Project)
        .select_from(join(Project, UserProjectLink, Project.project_id == UserProjectLink.project_id))
        .where(UserProjectLink.user_id == user.id)
    )
    if options:
        query = query.options(*options)
    return session.exec(query).all()

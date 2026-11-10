
import logging
import urllib.parse
from app.config import get_secret, get_settings, load_env_file
from app.general_utils import get_utc_datetime

logger = logging.getLogger(__name__)
from fastapi import HTTPException
from sqlalchemy import join
from sqlmodel import create_engine, select
# Need a .env file with the following variables
#DB_PASSWORD
#DB_USERNAME
#DB_SERVER
#DB_DATABASE

def setup_engine(env_file=None):
    load_env_file(env_file)
    password = get_secret('PGDB_PW')
    if password is None:
        return
    encoded_pw = urllib.parse.quote_plus(password)
    db = get_settings().db
    db_url = f'postgresql+psycopg2://{db.username}:{encoded_pw}@{db.server}/{db.database}'
    engine = create_engine(
        db_url,
        echo=False,
        pool_size=db.pool_size,
        max_overflow=db.max_overflow,
        pool_timeout=db.pool_timeout,
        pool_recycle=db.pool_recycle,
        pool_pre_ping=True,
        hide_parameters=True,
    )
    return(engine)


def only_update(session, cls, where, **kwargs):
    statement = select(cls).where(where)
    obj = session.exec(statement).first()
    if not obj:
        return None
    
    for key, value in kwargs.items():
        setattr(obj, key, value)
    session.add(obj)
    return obj

def add_or_update(session, cls, where, **kwargs):
    statement = select(cls).where(where)
    obj = session.exec(statement).first()
    if obj:
        for key, value in kwargs.items():
            setattr(obj, key, value)
    else:
        obj = cls(**kwargs)
    session.add(obj)
    return obj


def assert_mfid_available(session, mfid: str, resource_type: str) -> None:
    """Raise 409 if a client-supplied mfid already names any resource in the
    shared idtype table -- same-type or cross-type. Checked against the
    shared table, not just the caller's own, so a collision with a resource
    of a *different* type 409s here instead of reaching the add_or_update
    that creates the idtype row, which would silently flip the existing
    row's resource_type in place.

    resource_type is the kind being created now (e.g. "dataset"), used only
    to phrase the same-type message the way callers already expect.
    """
    from app.tables import Resource
    existing = session.exec(select(Resource).where(Resource.mfid == mfid)).one_or_none()
    if existing is None:
        return
    if existing.resource_type == resource_type:
        raise HTTPException(status_code=409,
                            detail=f"{resource_type.capitalize()} '{mfid}' already exists. Use PATCH to update it.")
    raise HTTPException(status_code=409,
                        detail=f"'{mfid}' is already in use by a {existing.resource_type}.")



def touch_resource_modification_time(session, mfid):
    """Bump Resource.modification_time for mfid to now, so clients polling
    modification_time_gte can detect that something about this resource changed
    (even if the change happened to a related object, e.g. a linked file or
    keyword, rather than the resource's own core fields).

    No-op (returns None) if no Resource row exists for mfid, so callers don't
    need to guard against not-yet-created or already-deleted resources.
    """
    from app.tables import Resource
    resource = session.exec(select(Resource).where(Resource.mfid == mfid)).one_or_none()
    if resource is not None:
        resource.modification_time = get_utc_datetime()
        session.add(resource)
    return resource


def sql_create_resource_acl(ag, mfid, read, write, session, permission):
    """Create an ACL entry. `permission` is the granted role; read/write are
    the pre-role columns, kept in step until they are dropped in phase 5."""
    from app.tables import ResourceACL
    from sqlmodel import select
    if ag is None:
        raise ValueError(f"Access group not found: cannot create ACL for resource '{mfid}'")
    acl = session.exec(
        select(ResourceACL)
        .where(ResourceACL.accessgroup_id == ag.id)
        .where(ResourceACL.mfid == mfid)
    ).one_or_none()
    if not acl:
        acl = ResourceACL(accessgroup_id=ag.id, mfid=mfid, read=read, write=write,
                          permission=permission.db_value)
        session.add(acl)
        session.flush()
    elif acl.permission is None:
        acl.permission = permission.db_value
        session.add(acl)
        session.flush()
    return acl


def create_standard_acls(session, mfid, owner_id, requestor_id, project_mfid=None):
    '''
    Create the standard set of ResourceACL entries for a newly created resource.
    - owner's ORCID group gets `owner`
    - project group gets `admin`, so project roles apply at face value
    Extra resource-specific ACLs (e.g. instrument group for datasets) should be added by the caller.

    No admin or read_only rows: platform admins bypass the ACL entirely and
    read_only is gone. Writing them would recreate the ~720k rows the migration
    just deleted.

    No requestor row either. The creator reaches the resource through the
    project group; a row per creator per resource expresses nothing.
    '''
    from app.tables import AccessGroup
    from app.utils.permissions import AccessGroupType, Role
    from sqlmodel import select

    if owner_id:
        from app.tables import User
        from app.utils.user_utils import identity_group_of

        owner = session.exec(select(User).where(User.unique_id == owner_id)).one_or_none()
        owner_ag = identity_group_of(session, owner.id) if owner is not None else None
        if owner_ag is None:
            raise HTTPException(
                status_code=409,
                detail=f"Owner '{owner_id}' has no personal access group",
            )
        sql_create_resource_acl(owner_ag, mfid, read=True, write=True,
                                session=session, permission=Role.OWNER)

    if project_mfid:
        proj_ag = session.exec(select(AccessGroup).where(
            AccessGroup.group_name == project_mfid,
            AccessGroup.group_type == AccessGroupType.PROJECT.value,
        )).one_or_none()
        if proj_ag is None:
            raise HTTPException(
                status_code=409,
                detail=f"Project '{project_mfid}' has no access group",
            )
        sql_create_resource_acl(proj_ag, mfid, read=True, write=True,
                                session=session, permission=Role.ADMIN)


def set_public(session, mfid: str, is_public: bool):
    """Add or remove the resource's ACL entry against the virtual public group, which is what
    "public" means. The only writer of public state, called when a record is created and by
    PUT and DELETE /resources/{mfid}/access/public."""
    from sqlalchemy import update
    from app.tables import AccessGroup, Dataset, Resource, ResourceACL
    from app.utils.permissions import PUBLIC_GROUP_NAME, AccessGroupType, Role
    from sqlmodel import select

    pub = session.exec(
        select(AccessGroup).where(
            AccessGroup.group_name == PUBLIC_GROUP_NAME,
            AccessGroup.group_type == AccessGroupType.SYSTEM.value,
        )
    ).one_or_none()
    if pub is None:
        if is_public:
            raise HTTPException(
                status_code=409,
                detail="Public access group is not provisioned",
            )
    # Older pods read these columns during a rollout, and Dataset.public overwrites the grant there.
    session.exec(update(Resource).where(Resource.mfid == mfid).values(public=is_public))
    session.exec(update(Dataset).where(Dataset.unique_id == mfid).values(public=is_public))
    if pub is None:
        return

    existing = session.exec(
        select(ResourceACL)
        .where(ResourceACL.mfid == mfid)
        .where(ResourceACL.accessgroup_id == pub.id)
    ).one_or_none()

    if is_public:
        if existing is None:
            existing = ResourceACL(
                mfid=mfid,
                accessgroup_id=pub.id,
                read=True,
                write=False,
                permission=Role.VIEWER.db_value,
            )
        else:
            existing.read = True
            existing.write = False
            existing.permission = Role.VIEWER.db_value
        session.add(existing)
    elif not is_public and existing is not None:
        session.delete(existing)


def get_active_deletion_request(session, resource_id: str):
    """Return the active (pending or approved) DeletionRequest for a resource, or None."""
    from app.tables import DeletionRequest
    return session.exec(
        select(DeletionRequest)
        .where(DeletionRequest.resource_id == resource_id)
        .where(DeletionRequest.status.in_(["pending", "approved"]))
    ).first()


def assert_not_pending_deletion(session, mfid: str) -> None:
    active = get_active_deletion_request(session, mfid)
    if active is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "pending_deletion",
                "status": active.status,
                "message": f"'{mfid}' has an active deletion request (status: {active.status}) and cannot be changed.",
            },
        )


def assert_parent_accepts_assignment(session, parent_mfid: str) -> None:
    """Refuse a parent pending deletion and share-lock its resource row until commit, so a
    deletion request on the parent cannot pass its blocker check while this assignment is in flight."""
    from app.tables import Resource
    session.exec(select(Resource.mfid).where(Resource.mfid == parent_mfid).with_for_update(read=True)).first()
    assert_not_pending_deletion(session, parent_mfid)


def select_active(resource_model, mfid, *columns, principal=None):
    """Select the record named `mfid` unless it has an active deletion request; with a principal,
    also unless that principal cannot read it. For filters, traversals, and lists."""
    query = select(*(columns or (resource_model,))).where(resource_model.unique_id == mfid).where(deletion_filter_for(resource_model))
    if principal is not None:
        from app.utils.auth_utils import permission_filter
        from app.utils.permissions import Permission
        query = query.where(permission_filter(principal, resource_model, Permission.READ))
    return query


def select_readable(resource_model, mfid, *columns, principal):
    """Select the record named `mfid` if `principal` can read it, even while it has an active
    deletion request. For exact lookups of a record and its own content."""
    from app.utils.auth_utils import permission_filter
    from app.utils.permissions import Permission
    return select(*(columns or (resource_model,))).where(resource_model.unique_id == mfid).where(permission_filter(principal, resource_model, Permission.READ))


def select_readable_file(file_mfid, principal):
    """Select (AssociatedFile, Dataset) for `file_mfid` if `principal` can read its dataset."""
    from app.tables import AssociatedFile, Dataset
    from app.utils.auth_utils import permission_filter
    from app.utils.permissions import Permission
    return (
        select(AssociatedFile, Dataset)
        .join(Dataset, Dataset.id == AssociatedFile.dataset_id)
        .where(AssociatedFile.mfid == file_mfid)
        .where(permission_filter(principal, Dataset, Permission.READ))
    )


def deletion_filter_for(resource_model):
    """
    Returns a SQLAlchemy filter expression that excludes resources with an active
    (pending or approved) deletion request. Apply with .where(deletion_filter_for(Model)).
    """
    from sqlalchemy import exists
    from app.tables import DeletionRequest
    return ~exists().where(
        DeletionRequest.resource_id == resource_model.unique_id,
        DeletionRequest.status.in_(["pending", "approved"])
    )


def visibility_filter_for(resource_model, visibility: str):
    """
    Returns a SQLAlchemy filter narrowing to public or private resources.
    `visibility` is 'public' or 'private'; callers skip this entirely for 'all'.

    Public means an ACL entry exists for the virtual public group.
    """
    is_public = public_grant_exists(resource_model.unique_id)
    return is_public if visibility == "public" else ~is_public


def public_grant_exists(mfid_column):
    """SQL expression for whether a record is public: an ACL entry for the virtual public group.
    Every response and filter reads this, never a `public` column."""
    from sqlalchemy import exists
    from app.tables import AccessGroup, ResourceACL
    from app.utils.permissions import PUBLIC_GROUP_NAME, AccessGroupType

    return exists().where(
        ResourceACL.mfid == mfid_column,
        ResourceACL.accessgroup_id == (
            select(AccessGroup.id).where(
                AccessGroup.group_name == PUBLIC_GROUP_NAME,
                AccessGroup.group_type == AccessGroupType.SYSTEM.value,
            ).scalar_subquery()
        ),
    )


def is_public(session, mfid: str) -> bool:
    from sqlalchemy import literal
    return bool(session.exec(select(public_grant_exists(literal(mfid)))).one())



def generate_kw_query(session, base_query, keyword_filter):
    from app.tables import Dataset, DatasetKeywordLink, Keyword

    keywords = session.exec(
        select(Keyword).where(Keyword.keyword.ilike(f"%{keyword_filter}%"))
    ).all()

    if not keywords:
        return []

    keyword_ids = [keyword.id for keyword in keywords]
    query = (
        base_query.select_from(
            join(DatasetKeywordLink, DatasetKeywordLink.dataset_id == Dataset.id)
        ).filter(DatasetKeywordLink.keyword_id.in_(keyword_ids))
    )
    return query

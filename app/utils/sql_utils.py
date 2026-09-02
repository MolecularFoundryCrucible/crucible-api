
import logging
import yaml
import os
import urllib.parse
from app.general_utils import get_secret, get_utc_isoformat, is_orcid

logger = logging.getLogger(__name__)
from fastapi import HTTPException
from sqlalchemy import join
from sqlmodel import create_engine, select
from dotenv import load_dotenv
# Need a .env file with the following variables
#DB_PASSWORD
#DB_USERNAME
#DB_SERVER
#DB_DATABASE

def setup_engine(env_file = '.env'):
    load_dotenv(env_file)
    password = get_secret('PGDB_PW', 'pgcloud_pw/versions/2')
    if password is None:
        return
    encoded_pw = urllib.parse.quote_plus(password)
    db_url = f'postgresql://{os.environ.get("DB_USERNAME")}:{encoded_pw}@{os.environ.get("DB_SERVER")}/{os.environ.get("DB_DATABASE")}'
    engine = create_engine(
        db_url,
        echo=False,
        pool_size=int(os.environ.get("DB_POOL_SIZE", "15")),
        max_overflow=int(os.environ.get("DB_MAX_OVERFLOW", "15")),
        pool_timeout=int(os.environ.get("DB_POOL_TIMEOUT", "30")),
        pool_recycle=int(os.environ.get("DB_POOL_RECYCLE", "1800")),
        pool_pre_ping=True,
    )
    return(engine)


def get_any(d, keys, default=None):
    for k in keys: 
        if k in d:
           return d[k]
    return default

def table_obj_to_dict(obj):
    d = dict(obj)
    if not 'id' in d.keys():
        return(d)
    elif d['id'] is not None:
        return(d)
    else:
        d.pop('id')
        return(d)
    
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
    from app.models.core_models import Resource
    existing = session.exec(select(Resource).where(Resource.mfid == mfid)).one_or_none()
    if existing is None:
        return
    if existing.resource_type == resource_type:
        raise HTTPException(status_code=409,
                            detail=f"{resource_type.capitalize()} '{mfid}' already exists. Use PATCH to update it.")
    raise HTTPException(status_code=409,
                        detail=f"'{mfid}' is already in use by a {existing.resource_type}.")



def delete_link(table, dataset_id, session):
    statement = select(table).where(table.dataset_id == dataset_id)
    found_records = session.exec(statement).all()
    for rec in found_records:
        session.delete(rec)


def touch_resource_modification_time(session, mfid):
    """Bump Resource.modification_time for mfid to now, so clients polling
    modification_time_gte can detect that something about this resource changed
    (even if the change happened to a related object, e.g. a linked file or
    keyword, rather than the resource's own core fields).

    No-op (returns None) if no Resource row exists for mfid, so callers don't
    need to guard against not-yet-created or already-deleted resources.
    """
    from app.models.core_models import Resource
    resource = session.exec(select(Resource).where(Resource.mfid == mfid)).one_or_none()
    if resource is not None:
        resource.modification_time = get_utc_isoformat()
        session.add(resource)
    return resource


def sql_create_resource_acl(ag, mfid, read, write, session, permission):
    """Create an ACL entry. `permission` is the granted role; read/write are
    the pre-role columns, kept in step until they are dropped in phase 5."""
    from app.models.core_models import ResourceACL
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


def create_standard_acls(session, mfid, owner_id, requestor_id, project_id=None):
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
    from app.models.core_models import AccessGroup, Project
    from app.utils.permissions import AccessGroupType, Role
    from sqlmodel import select

    if owner_id:
        owner_ag = session.exec(select(AccessGroup).where(
            AccessGroup.group_name == owner_id,
            AccessGroup.group_type == AccessGroupType.IDENTITY.value,
        )).one_or_none()
        if owner_ag is None:
            raise HTTPException(
                status_code=409,
                detail=f"Owner '{owner_id}' has no personal access group",
            )
        sql_create_resource_acl(owner_ag, mfid, read=True, write=True,
                                session=session, permission=Role.OWNER)

    if project_id:
        project = session.exec(select(Project).where(Project.project_id == project_id)).one_or_none()
        proj_ag = (
            session.exec(select(AccessGroup).where(
                AccessGroup.group_name == project.unique_id,
                AccessGroup.group_type == AccessGroupType.PROJECT.value,
            )).one_or_none()
            if project else None
        )
        if proj_ag is None:
            raise HTTPException(
                status_code=409,
                detail=f"Project '{project_id}' has no access group",
            )
        sql_create_resource_acl(proj_ag, mfid, read=True, write=True,
                                session=session, permission=Role.ADMIN)


def set_public(session, mfid: str, is_public: bool):
    """Add or remove the resource's ACL entry against the virtual public group.

    Publishing is an ACL grant now, not a boolean. The Resource.public column is
    still written alongside until phase 5 drops it, but nothing reads it for
    authorization — so setting the flag without this leaves a resource marked
    public that nobody outside can actually reach.
    """
    from app.models.core_models import AccessGroup, ResourceACL
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
    from app.models.core_models import DeletionRequest
    return session.exec(
        select(DeletionRequest)
        .where(DeletionRequest.resource_id == resource_id)
        .where(DeletionRequest.status.in_(["pending", "approved"]))
    ).first()


def deletion_filter_for(resource_model):
    """
    Returns a SQLAlchemy filter expression that excludes resources with an active
    (pending or approved) deletion request. Apply with .where(deletion_filter_for(Model)).
    """
    from sqlalchemy import exists
    from app.models.core_models import DeletionRequest
    return ~exists().where(
        DeletionRequest.resource_id == resource_model.unique_id,
        DeletionRequest.status.in_(["pending", "approved"])
    )


def visibility_filter_for(resource_model, visibility: str):
    """
    Returns a SQLAlchemy filter narrowing to public or private resources.
    `visibility` is 'public' or 'private'; callers skip this entirely for 'all'.

    Public means an ACL entry exists for the virtual public group — the ACL
    entry is authoritative, not the legacy `public` column that phase 5 drops.
    """
    from sqlalchemy import exists
    from app.models.core_models import AccessGroup, ResourceACL
    from app.utils.permissions import PUBLIC_GROUP_NAME, AccessGroupType

    is_public = exists().where(
        ResourceACL.mfid == resource_model.unique_id,
        ResourceACL.accessgroup_id == (
            select(AccessGroup.id).where(
                AccessGroup.group_name == PUBLIC_GROUP_NAME,
                AccessGroup.group_type == AccessGroupType.SYSTEM.value,
            ).scalar_subquery()
        ),
    )
    return is_public if visibility == "public" else ~is_public



def generate_kw_query(session, base_query, keyword_filter):
    from app.models.core_models import Dataset, DatasetKeywordLink, Keyword

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

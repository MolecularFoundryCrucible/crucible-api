
import yaml
import os
import urllib.parse
from app.general_utils import get_secret, is_orcid
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
    engine = create_engine(db_url, echo=False)
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



def delete_link(table, dataset_id, session):
    statement = select(table).where(table.dataset_id == dataset_id)
    found_records = session.exec(statement).all()
    for rec in found_records:
        session.delete(rec)


def sql_create_resource_acl(ag, mfid, read, write, session):
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
        acl = ResourceACL(accessgroup_id=ag.id, mfid=mfid, read=read, write=write)
        session.add(acl)
        session.flush()
    return acl


def create_standard_acls(session, mfid, owner_id, requestor_id, project_id=None):
    '''
    Create the standard set of ResourceACL entries for a newly created resource.
    - admin and read_only groups always get access
    - owner gets read+write (if provided)
    - requestor gets read+write if different from owner (e.g. service account acting on behalf)
    - project group gets read+write (if provided)
    Extra resource-specific ACLs (e.g. instrument group for datasets) should be added by the caller.
    '''
    from app.models.core_models import AccessGroup
    from sqlmodel import select

    admin = session.exec(select(AccessGroup).where(AccessGroup.group_name == "admin")).one_or_none()
    sql_create_resource_acl(admin, mfid, read=True, write=True, session=session)

    ro = session.exec(select(AccessGroup).where(AccessGroup.group_name == "read_only")).one_or_none()
    sql_create_resource_acl(ro, mfid, read=True, write=False, session=session)

    if owner_id:
        owner_ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == owner_id)).one_or_none()
        sql_create_resource_acl(owner_ag, mfid, read=True, write=True, session=session)

    if requestor_id and is_orcid(requestor_id) and requestor_id != owner_id:
        requestor_ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == requestor_id)).one_or_none()
        sql_create_resource_acl(requestor_ag, mfid, read=True, write=True, session=session)

    if project_id:
        proj_ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == project_id)).one_or_none()
        sql_create_resource_acl(proj_ag, mfid, read=True, write=True, session=session)


def get_active_deletion_request(session, resource_id: str):
    """Return the active (pending or approved) DeletionRequest for a resource, or None."""
    from app.models.core_models import DeletionRequest
    return session.exec(
        select(DeletionRequest)
        .where(DeletionRequest.resource_id == resource_id)
        .where(DeletionRequest.status.in_(["pending", "approved"]))
    ).first()


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



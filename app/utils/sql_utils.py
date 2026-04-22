
import yaml
import os
import urllib.parse
from app.general_utils import get_secret
from sqlalchemy import join
from sqlmodel import create_engine, select
from app.general_utils import  get_secret
from dotenv import load_dotenv
# Need a .env file with the following variables
#DB_PASSWORD
#DB_USERNAME
#DB_SERVER
#DB_DATABASE

def setup_engine(env_file = '.env'):
    print(load_dotenv(env_file))
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
    obj = session.exec(statement).one_or_none()
    print(obj)
    if not obj:
        return None
    
    for key, value in kwargs.items():
        print(key)
        setattr(obj, key, value)
    session.add(obj)
    return obj

def add_or_update(session, cls, where, **kwargs):
    statement = select(cls).where(where)
    obj = session.exec(statement).one_or_none()
    print(obj)
    if obj: 
        for key, value in kwargs.items():
            print(key,value)
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
        return None
    acl = session.exec(
        select(ResourceACL)
        .where(ResourceACL.accessgroup_id == ag.id)
        .where(ResourceACL.mfid == mfid)
    ).one_or_none()
    if not acl:
        acl = ResourceACL(accessgroup_id=ag.id, mfid=mfid, read=read, write=write)
        session.add(acl)
        session.commit()
        session.refresh(acl)
    return acl


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
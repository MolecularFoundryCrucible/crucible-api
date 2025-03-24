
import yaml
import os
from app.general_utils import get_secret
from sqlmodel import create_engine, select



def setup_engine(env_config_file = None):
    password = get_secret('PGDB_PW', 'projects/776258882599/secrets/pgcloud_pw/versions/1')
    if env_config_file is not None:
        db_url = parse_db_url_from_config_file(env_config_file, password)
    else:
        db_type = os.getenv("DB_TYPE")
        if db_type == 'sqlite':
            db_url = f'sqlite:///{os.getenv("DB_FILE")}'
        elif db_type == 'postgres':
            db_url = f'postgresql://{os.getenv("DB_USERNAME")}:{password}@{os.getenv("DB_SERVER")}/{os.getenv("DB_DATABASE")}'
        else:
            raise ValueError(f"Unknown DB Type {db_type}")
        
    engine = create_engine(db_url, echo=False)
    return(engine)


def parse_db_url_from_config_file(env_config_file, password):
    with open(env_config_file) as f:
        env_config = yaml.safe_load(f)
    
    C = env_config.copy()

    if C['db_type'] == 'sqlite':
        db_url = f"sqlite:///{C['db_file']}"
    elif C['db_type'] == 'postgres':
        db_url = f"postgresql://{C['db_username']}:{password}@{C['db_server']}/{C['db_database']}"
    else:
        raise ValueError(f"Unknown DB Type {C['db_type']}")
    return(db_url)


def get_any(d, keys, default=None):
    for k in keys: 
        if k in d:
           return d[k]
    return default


def add_or_update(session, cls, where, **kwargs):
    statement = select(cls).where(where)
    obj = session.exec(statement).one_or_none()
    print(type(obj))
    if obj: 
        for key, value in kwargs.items():
            print(key)
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
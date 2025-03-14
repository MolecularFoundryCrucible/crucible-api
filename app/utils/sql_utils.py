
import yaml
from utils import get_secret_2
from sqlmodel import create_engine, select



def setup_engine(env_config_file):
    with open(f"conf/{env_config_file}") as f:
        env_config = yaml.safe_load(f)
    
    C = config = env_config.copy()
    password = get_secret_2('PGDB_PW', 'projects/776258882599/secrets/pgcloud_pw/versions/1')
    
    if C['db_type'] == 'sqlite':
        db_url = f"sqlite:///{C['db_file']}"
    elif C['db_type'] == 'postgres':
        db_url = f"postgresql://{C['db_username']}:{password}@{C['db_server']}/{C['db_database']}"
    else:
        raise ValueError(f"Unknown DB Type {C['db_type']}")

    engine = create_engine(db_url, echo=False)
    return(engine)


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
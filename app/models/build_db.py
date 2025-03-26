
from sqlmodel import  SQLModel
from app.models.core_models import *
from app.models.instrument import *
from app.models.misc import *
from app.models.tasks import *
from app.utils.sql_utils import setup_engine

engine = setup_engine("conf/crucible-db-local.yaml")
SQLModel.metadata.create_all(engine)


# python -m app.models.build_db
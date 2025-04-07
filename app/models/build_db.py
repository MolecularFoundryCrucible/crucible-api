
import os
from sqlmodel import  SQLModel
from app.models.core_models import *
# from app.models.instrument import *
# from app.models.misc import *
# from app.models.tasks import *
from app.utils.sql_utils import setup_engine
home = os.getenv("HOME")
engine = setup_engine(f"{home}/Git/CrucibleRefactor/crucible-api/app/conf/crucible-db-local.yaml")
SQLModel.metadata.create_all(engine)


# python -m app.models.build_db
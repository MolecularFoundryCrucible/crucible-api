
import os
from sqlmodel import  SQLModel
import sys
home = os.getenv("HOME")

#sys.path.append(f"{home}/git/crucible-api/")
#from app.models.sample_metadata_models import *
from app.models.core_models import *
from app.models.instrument import *
from app.models.misc import *
from app.models.tasks import *
from app.models.deletion import *
from app.utils.sql_utils import setup_engine
engine = setup_engine(f"{home}/Git/crucible-api/app/conf/crucible-db-staging-local.env")
SQLModel.metadata.create_all(engine)


#python -m app.models.build_db

"""
import os
from sqlmodel import  SQLModel
import sys
home = os.getenv("HOME")

db_config_file = "local_db_config.yaml"

#sys.path.append(f"{home}/git/crucible-api/")
#from app.models.sample_metadata_models import *
# from app.models.core_models import *
# from app.models.instrument import *
# from app.models.misc import *
# from app.models.tasks import *
from app.utils.sql_utils import setup_engine

#engine = setup_engine(f"{home}/git/crucible-api/app/conf/crucible-db-local.yaml")
engine = setup_engine(f"{home}/crucible-api/app/conf/{db_config_file}")

SQLModel.metadata.create_all(engine)


# python -m app.models.build_db
"""

import os
from sqlmodel import SQLModel, create_engine
import sys


from app.models.core_models import NewScientificMetadata 

def build_db():
    database_url = "postgresql://crucible-api-user:cdsetrpl@127.0.0.1:5433/crucible-staging"
    
    print(f"Connecting to: {database_url}")
    engine = create_engine(database_url)
    
    # --- 4. Create the table ---
    print("Creating table 'scientific_metadata' (NewScientificMetadata)...")
    SQLModel.metadata.create_all(engine)
    print("Done! Table created successfully.")

if __name__ == "__main__":
    build_db()
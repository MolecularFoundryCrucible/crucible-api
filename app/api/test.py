import os
import sys
sys.path.append('/home/morgan/Git/CrucibleRefactor/crucible-api/')
from sqlmodel import Session, select
from sqlalchemy import join
from app.utils.sql_utils import setup_engine


from app.models.core_models import (Dataset, 
                                    DatasetACL,
                                    Keyword,
                                    DatasetKeywordLink,
                                    Thumbnail,
                                    AssociatedFile,
                                    User,
                                    AccessGroup,
                                    Project,
                                    UserAccessGroupLink,
                                    UserProjectLink,
                                    DatasetCollection,
                                    DatasetCollectionLink,
                                    ScientificMetadata)

from app.models.instrument import Instrument
from app.models.misc import IdType, DataDriveLocation, GoogleDrives
from app.models.tasks import GoogleDriveTransferRequest, SciCatUpdateRequest, IngestionRequest

connect_args = {"check_same_thread": False}

home_dir = os.getenv("HOME")
db_config_tmp = os.getenv("DB_CONF_FILE")
db_config_file = f"{home_dir}/{db_config_tmp}"

engine = setup_engine(db_config_file)

dsid = "04qed8jsxd3avcgk7d443rw7t4"
orcid = "0009-0001-9493-2006"
with Session(engine) as session:
    ag = session.exec(select(UserAccessGroupLink).select_from(join(UserAccessGroupLink, User, User.id == UserAccessGroupLink.user_id)).where(User.orcid == orcid)).all()
    ag_ids = [x.accessgroup_id for x in ag]
    print(ag_ids)
with Session(engine) as session:
    ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id)).where(Dataset.unique_id == dsid).filter(DatasetACL.accessgroup_id.in_(ag_ids)).distinct()
    ds = session.exec(ds_cmd).one_or_none()
    print(ds)

import json
import os
from pathlib import Path
from typing import Annotated, List, Optional, Union

import pytz
import networkx as nx
from networkx.readwrite import json_graph
from fastapi import APIRouter, Body, Depends, File, HTTPException, Request, UploadFile
from sqlalchemy import exists, func, join, text
from sqlalchemy.orm import joinedload, selectinload, attributes
from sqlmodel import and_, or_, Session, select

from app.api.dependencies import get_engine
from app.general_utils import get_secret, get_tz_isoformat, checkhash
from app.models.core_models import (
    AccessGroup,
    AssociatedFile,
    Dataset,
    ResourceACL,
    DatasetKeywordLink,
    DatasetLink,
    DatasetSampleLink,
    DeletionRequest,
    IngestionRequest,
    Instrument,
    Keyword,
    Project,
    Resource,
    Sample,
    ScientificMetadata,
    NewScientificMetadata,
    Thumbnail,
    User,
)
from app.models.response_schemas import DatasetSampleLinkResponse
from app.models.datasets import (
    AssociatedFileCreate, AssociatedFileRead,
    DatasetCreate, DatasetRead, DatasetResponse, DatasetUpdate,
    ThumbnailCreate, ThumbnailRead,
)
from app.models.access_groups import AccessGroupRead
from app.models.samples import SampleRead
from app.utils.auth_utils import authorize_user, require_groups
from app.utils.rmq_utils import send_pika_message
from app.utils.sql_utils import add_or_update, only_update, generate_kw_query, sql_create_resource_acl

router = APIRouter(tags=["datasets"])
engine = get_engine()

# eventually could switch to pushing + using github tags
with open('pyproject.toml', 'r') as f:
    for line in f:
        if line.startswith('version = '):
            version_line = line
            break
    version = version_line.split('=')[1].strip().strip('"')
    API_VERSION = f'v{version}'

#API_VERSION = os.getenv('API_VERSION', None)

# if API_VERSION is None and BRANCH_NAME == 'v1':
#     API_VERSION = 'v1.0.0'
# elif API_VERSION is None:
#     API_VERSION = 'dev'
# else:
#     API_VERSION = API_VERSION

API_VERSION_GITHASH = os.getenv("GITHASH", None)


@router.post("/datasets/{dsid}/carrier_segmentation")
def request_carrier_segmentation(dsid, auth_res: Annotated[tuple, Depends(authorize_user)]):
    rmq_pw = get_secret("RABBITMQ_DEFAULT_PW", "rabbitmq_default_pw/versions/1")
    msg = {
        "dsid": dsid,
    }

    send_pika_message(
        message=json.dumps(msg),
        exchange="",
        routing_key="carrier-segmentation",
        user="admin",
        rmq_pw=rmq_pw
    )
    return

@router.post("/datasets/{dsid}/insitu_spec_aggregation")
def request_insitu_aggregation(dsid, auth_res: Annotated[tuple, Depends(authorize_user)]):
    rmq_pw = get_secret("RABBITMQ_DEFAULT_PW", "rabbitmq_default_pw/versions/1")
    msg = {
        "dsid": dsid,
    }

    send_pika_message(
        message=json.dumps(msg),
        exchange="",
        routing_key="insitu-aggregation",
        user="admin",
        rmq_pw=rmq_pw
    )
    return

@router.post("/datasets", response_model=DatasetRead)
async def create_dataset(
    auth_res: Annotated[tuple, Depends(authorize_user)],
    dataset: DatasetCreate
):
    print(f"received {dataset}")
    dataset_details = dataset.model_dump()
    user_unique_id, ag_list = auth_res
    with Session(engine) as session:
        if not dataset.owner_orcid:
            dataset_details['owner_orcid'] = user_unique_id

        if dataset_details.get('owner_orcid', None) is not None:
            user = session.exec(select(User).where(User.unique_id == dataset_details['owner_orcid'])).one_or_none()
            dataset_details['owner_user_id'] = user.id if user else None

        if dataset_details.get('instrument_name', None):
            instrument = session.exec(select(Instrument).where(Instrument.instrument_name == dataset_details['instrument_name'])).one_or_none()
            dataset_details['instrument_id'] = instrument.id if instrument else None

        ds = add_or_update(
            session,
            Dataset,
            Dataset.unique_id == dataset_details['unique_id'],
            **dataset_details
        )

        session.commit()
        session.refresh(ds)

        try:
            print(f'Resource mfid being written: {ds.unique_id}; {API_VERSION}; {API_VERSION_GITHASH}')
            now = get_tz_isoformat()
            ri = add_or_update(session,
                        Resource,
                        where=(Resource.mfid == ds.unique_id),
                        mfid = ds.unique_id,
                        resource_type = "dataset")
            
            ri.creation_time = now
            ri.modification_time = now
            ri.created_with_api_version = API_VERSION
            ri.api_version_githash = API_VERSION_GITHASH

            print('added the Resource entry')
            session.commit()
            print('committed')
            session.refresh(ds)
            print(f"#=========== ID TYPE ADDED for dataset {ds.unique_id=}")

        except Exception as e:
            print(f"#=========== ID TYPE FAILED for dataset {ds.unique_id=}: {e}")

        print(f'added dataset {ds}')
        # set appropriate access controls
        admin = session.exec(select(AccessGroup).where(AccessGroup.group_name == "admin")).one_or_none()
        sql_create_resource_acl(admin, ds.unique_id, read=True, write=True, session=session)

        ro = session.exec(select(AccessGroup).where(AccessGroup.group_name == "read_only")).one_or_none()
        sql_create_resource_acl(ro, ds.unique_id, read=True, write=False, session=session)
        print(f"add ro access to {ds.unique_id}")

        if dataset_details.get('owner_orcid', None) is not None:
            user_ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == dataset_details['owner_orcid'])).one_or_none()
            sql_create_resource_acl(user_ag, ds.unique_id, read=True, write=True, session=session)
            print(f'added owner orcid access to {ds.unique_id}')

        if user_unique_id != dataset_details.get('owner_orcid', None):
            requestor_ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == user_unique_id)).one_or_none()
            sql_create_resource_acl(requestor_ag, ds.unique_id, read=True, write=True, session=session)
            print(f'added requestor orcid access to {ds.unique_id}')

        if dataset_details.get('project_id', None) is not None:
            prop_ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == dataset_details['project_id'])).one_or_none()
            sql_create_resource_acl(prop_ag, ds.unique_id, read=True, write=True, session=session)
            print(f'added proposal access to {ds.unique_id}')

        if dataset_details.get('instrument_name', None) is not None:
            ag_query = select(AccessGroup).where(AccessGroup.group_name == dataset_details['instrument_name'].lower())
            inst_ag = session.exec(ag_query).one_or_none()
            if inst_ag is None:
                inst_ag = add_or_update(session,
                                        AccessGroup,
                                        AccessGroup.group_name == dataset_details['instrument_name'].lower(),
                                        group_name=dataset_details['instrument_name'].lower())
            sql_create_resource_acl(inst_ag, ds.unique_id, read=True, write=True, session=session)
            print(f'added instrument group access to {ds.unique_id}')

    return ds


@router.patch("/datasets/{dsid}", response_model=DatasetRead)
def update_dataset(auth_res: Annotated[tuple, Depends(authorize_user)], dsid: str, updated_dataset: DatasetUpdate):
    '''
    Updates a dataset in SQL and returns the Updated Dataset object.
    Sends a message to RabbitMQ to alert RMQ that updates have been made.
    '''
    user_unique_id, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]
    ds_cmd = select(Dataset).select_from(
                                join(Dataset, ResourceACL, ResourceACL.mfid == Dataset.unique_id)).where(
                                    Dataset.unique_id == dsid).filter(
                                        ResourceACL.accessgroup_id.in_(ag_ids)).distinct()

    with Session(engine) as session:
        ds_list = session.exec(ds_cmd).all()

    if not ds_list:
        raise HTTPException(status_code=404, detail="Dataset not found")


    with Session(engine) as session:
        stored_ds = session.exec(select(Dataset).where(Dataset.unique_id == dsid)).one_or_none()
        if stored_ds:
            stored_ds_dict = dict(stored_ds)
        print(f"{stored_ds_dict=}")

        update_attr = updated_dataset.model_dump(exclude_unset=True)
        print(f"{update_attr=}")
        filtered_updates = {k: v for k, v in update_attr.items()}


        upds = add_or_update(session, Dataset, Dataset.unique_id == dsid, **filtered_updates)

        ri = session.exec(select(Resource).where(Resource.mfid == dsid)).one_or_none()
        if ri:
            ri.modification_time = get_tz_isoformat()

        if upds.owner_user_id is not None:
            user_orcid = session.exec(select(User.unique_id).where(User.id == upds.owner_user_id)).one_or_none()
            user_ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == user_orcid)).one_or_none()
            sql_create_resource_acl(user_ag, upds.unique_id, read=True, write=True, session=session)
            print('add owner orcid access')

        if upds.project_id is not None:
            prop_name = session.exec(select(Project.project_id).where(Project.project_id == upds.project_id)).one_or_none()
            prop_ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == prop_name)).one_or_none()
            sql_create_resource_acl(prop_ag, upds.unique_id, read=True, write=True, session=session)
            print('add proposal access')

        if upds.instrument_name is not None:
            inst_ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == upds.instrument_name.lower())).one_or_none()
            sql_create_resource_acl(inst_ag, upds.unique_id, read=True, write=True, session=session)
            print('add instrument group access')

        session.commit()
        session.refresh(upds)
        return upds


@router.get("/datasets", response_model=List[DatasetResponse], response_model_exclude_none=True)
def list_datasets(auth_res: Annotated[tuple, Depends(authorize_user)], request: Request, include_metadata: bool = False, limit: Optional[int] = None,
                  creation_time_gte: Optional[str] = None, creation_time_lte: Optional[str] = None,
                  modification_time_gte: Optional[str] = None, modification_time_lte: Optional[str] = None):
    '''
    Returns a list of all dataset IDs the requesting user is authorized to view.
    If no dataset IDs are authorized for the user, an empty list will be returned.

    Args:
        auth_res: Authorization result containing access group info
        request: Request object containing query parameters
        limit: Optional maximum number of datasets to return
        creation_time_gte: Filter datasets created on or after this ISO timestamp
        creation_time_lte: Filter datasets created on or before this ISO timestamp
        modification_time_gte: Filter datasets modified on or after this ISO timestamp
        modification_time_lte: Filter datasets modified on or before this ISO timestamp
    '''
    user_unique_id, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]
    params_dict = dict(request.query_params)
    keyword_filter = params_dict.pop('keyword', None)
    # Remove explicit query params so they don't get applied as Dataset attribute filters
    for k in ('creation_time_gte', 'creation_time_lte', 'modification_time_gte', 'modification_time_lte',
              'include_metadata', 'limit'):
        params_dict.pop(k, None)
    print(f'{include_metadata=}')
    with Session(engine) as session:
        # EXISTS subquery for ACL check — avoids JOIN fanout (one row per ACL entry)
        # and eliminates the need for DISTINCT on the outer query.
        acl_subquery = exists().where(
            ResourceACL.mfid == Dataset.unique_id,
            ResourceACL.accessgroup_id.in_(ag_ids)
        )
        auth_filter = (Dataset.public == True) | acl_subquery

        if include_metadata:
            query = (
                select(Dataset)
                .options(selectinload(Dataset.scientific_metadata), selectinload(Dataset.resource))
                .join(Resource, Resource.mfid == Dataset.unique_id, isouter=True)
                .where(auth_filter)
            )
        else:
            query = (
                select(Dataset)
                .options(selectinload(Dataset.resource))
                .join(Resource, Resource.mfid == Dataset.unique_id, isouter=True)
                .where(auth_filter)
            )

        # Exclude soft-deleted resources (pending or approved deletion)
        query = query.where(
            ~exists().where(
                DeletionRequest.resource_id == Dataset.unique_id,
                DeletionRequest.status.in_(["pending", "approved"])
            )
        )

        # Filter on Resource timestamps
        if creation_time_gte:
            query = query.where(Resource.creation_time >= creation_time_gte)
        if creation_time_lte:
            query = query.where(Resource.creation_time <= creation_time_lte)
        if modification_time_gte:
            query = query.where(Resource.modification_time >= modification_time_gte)
        if modification_time_lte:
            query = query.where(Resource.modification_time <= modification_time_lte)

        # join and filter on keywords
        if keyword_filter:
            query = generate_kw_query(session, query, keyword_filter)

        # Apply additional filters for Dataset attributes
        for key, value in params_dict.items():
            if hasattr(Dataset, key):
                query = query.where(getattr(Dataset, key) == value)

        # Apply limit if specified
        if limit is not None:
            query = query.limit(limit)

        results = session.exec(query).all()
        return [
            DatasetResponse.model_validate({
                **dataset.model_dump(),
                "creation_time": dataset.resource.creation_time if dataset.resource else None,
                "modification_time": dataset.resource.modification_time if dataset.resource else None,
                "scientific_metadata": (
                    dataset.scientific_metadata.scientific_metadata
                    if include_metadata and dataset.scientific_metadata else None
                ),
            })
            for dataset in results
        ]


@router.get("/datasets/{dsid}", response_model=DatasetResponse, response_model_exclude_none=True)
def read_dataset(auth_res: Annotated[tuple, Depends(authorize_user)], dsid: str):
    '''
    Returns a dataset found with the provided unique ID
    if one is exists and the requesting user is
    authorized to view the dataset. Otherwise, returns None
    '''
    user_unique_id, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        ds_cmd = select(Dataset).options(selectinload(Dataset.resource)).select_from(join(Dataset, ResourceACL, ResourceACL.mfid == Dataset.unique_id, isouter=True)).where(Dataset.unique_id == dsid).filter((ResourceACL.accessgroup_id.in_(ag_ids)) | (Dataset.public == True)).distinct()
        ds = session.exec(ds_cmd).one_or_none()
        if ds is None:
            return None
        return DatasetResponse.model_validate({
            **ds.model_dump(),
            "creation_time": ds.resource.creation_time if ds.resource else None,
            "modification_time": ds.resource.modification_time if ds.resource else None,
        })


@router.post("/datasets/{parent_dataset_id}/children/{child_dataset_id}", response_model=DatasetLink)
def link_parent_child(parent_dataset_id, child_dataset_id, auth_res: Annotated[tuple, Depends(authorize_user)]):
    user_unique_id, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]
    ds_cmd = select(Dataset).select_from(
                                        join(Dataset,
                                                ResourceACL,
                                                ResourceACL.mfid == Dataset.unique_id, isouter=True)
                                        ).filter(
                                                (ResourceACL.accessgroup_id.in_(ag_ids)) |
                                                (Dataset.public == True)
                                        )

    with Session(engine) as session:
        parent = session.exec(ds_cmd.where(Dataset.unique_id == parent_dataset_id)).first()
        if not parent:
            raise HTTPException(status_code=404, detail="Parent dataset not found")

        child = session.exec(ds_cmd.where(Dataset.unique_id == child_dataset_id)).first()
        if not child:
            raise HTTPException(status_code=404, detail="Child dataset not found")

        link = add_or_update(session,
                     DatasetLink,
                     where = (and_(DatasetLink.parent_dataset_id == parent.id,
                                   DatasetLink.child_dataset_id == child.id)),
                     parent_dataset_id = parent.id,
                     child_dataset_id = child.id)

        session.commit()
        session.refresh(link)

        return(link)


@router.delete("/datasets/{parent_dataset_id}/children/{child_dataset_id}")
def unlink_parent_child(parent_dataset_id: str, child_dataset_id: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    _, ag_list = auth_res
    require_groups(ag_list)
    with Session(engine) as session:
        parent = session.exec(select(Dataset).where(Dataset.unique_id == parent_dataset_id)).one_or_none()
        if not parent:
            raise HTTPException(status_code=404, detail="Parent dataset not found")

        child = session.exec(select(Dataset).where(Dataset.unique_id == child_dataset_id)).one_or_none()
        if not child:
            raise HTTPException(status_code=404, detail="Child dataset not found")

        link = session.exec(
            select(DatasetLink).where(
                and_(DatasetLink.parent_dataset_id == parent.id,
                     DatasetLink.child_dataset_id == child.id)
            )
        ).one_or_none()

        if link is None:
            raise HTTPException(status_code=404, detail="No link found between these datasets")

        session.delete(link)
        session.commit()
        return {"message": f"Successfully unlinked {parent_dataset_id} -> {child_dataset_id}"}


@router.get("/datasets/{child_dataset_id}/parents", response_model=List[DatasetRead])
def list_parent_datasets(child_dataset_id: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    '''
    Returns a list of parent datasets for a given dataset
    '''
    user_unique_id, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]
    with Session(engine) as session:
        ds_cmd = select(Dataset).select_from(
                                            join(Dataset,
                                                    ResourceACL,
                                                    ResourceACL.mfid == Dataset.unique_id, isouter=True)
                                            ).filter(
                                                    (ResourceACL.accessgroup_id.in_(ag_ids)) |
                                                    (Dataset.public == True)
                                            ).distinct()
        child = session.exec(ds_cmd.where(Dataset.unique_id == child_dataset_id)).one_or_none()

        if child is None:
            raise HTTPException(status_code=404, detail="Child dataset not found")

        # TODO: JOIN ON ACL BUT PERMISSIONS SHOULD PROBABLY INHERIT..
        parents = session.exec(
            select(Dataset)
            .select_from(join(Dataset, DatasetLink, DatasetLink.parent_dataset_id == Dataset.id))
            .where(DatasetLink.child_dataset_id == child.id)
            .where(
                ~exists().where(
                    DeletionRequest.resource_id == Dataset.unique_id,
                    DeletionRequest.status.in_(["pending", "approved"])
                )
            )
        ).all()
        return parents


@router.get("/datasets/{parent_dataset_id}/children", response_model=List[DatasetRead])
def list_child_datasets(parent_dataset_id: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    '''
    Returns a list of child datasets for a given parent dataset
    '''
    user_unique_id, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]
    with Session(engine) as session:
        ds_cmd = select(Dataset).select_from(
                                            join(Dataset,
                                                    ResourceACL,
                                                    ResourceACL.mfid == Dataset.unique_id, isouter=True)
                                            ).filter(
                                                    (ResourceACL.accessgroup_id.in_(ag_ids)) |
                                                    (Dataset.public == True)
                                            ).distinct()

        parent = session.exec(ds_cmd.where(Dataset.unique_id == parent_dataset_id)).one_or_none()

        if not parent:
            raise HTTPException(status_code=404, detail="Parent sample not found")

        # TODO: JOIN ON ACL BUT PERMISSIONS SHOULD PROBABLY INHERIT..
        children = session.exec(
            select(Dataset)
            .select_from(join(Dataset, DatasetLink, DatasetLink.child_dataset_id == Dataset.id))
            .where(DatasetLink.parent_dataset_id == parent.id)
            .where(
                ~exists().where(
                    DeletionRequest.resource_id == Dataset.unique_id,
                    DeletionRequest.status.in_(["pending", "approved"])
                )
            )
        ).all()
        return children


@router.delete("/datasets/{dsid}")
def delete_dataset(auth_res: Annotated[tuple, Depends(authorize_user)], dsid: str):
    raise HTTPException(status_code=501, detail="Hard deletion not implemented. Use the deletion request system.")


@router.delete('/datasets/{dsid}/samples/{sample_id}')
def delete_dataset_sample_link(auth_res: Annotated[tuple, Depends(authorize_user)], dsid: str, sample_id: str):
    _, ag_list = auth_res
    require_groups(ag_list)
    ds_cmd = select(Dataset).where(Dataset.unique_id == dsid)
    samp_cmd = select(Sample).where(Sample.unique_id == sample_id)

    with Session(engine) as session:
        ds = session.exec(ds_cmd).one_or_none()
        samp = session.exec(samp_cmd).one_or_none()

        if samp is None:
            raise HTTPException(status_code=404, detail="Sample not found")
        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found or access denied")

        # Delete the link
        link = session.exec(
            select(DatasetSampleLink).where(
                and_(DatasetSampleLink.dataset_id == ds.id, DatasetSampleLink.sample_id == samp.id)
            )
        ).one_or_none()

        if link is None:
            return {"warning": f"No link found between dataset {dsid} and sample {sample_id}"}

        session.delete(link)
        session.commit()

        return {"message": f"Successfully deleted link between dataset {dsid} and sample {sample_id}"}


@router.get('/datasets/{dsid}/download_links')
def generate_signed_url_download_link(auth_res: Annotated[tuple, Depends(authorize_user)], dsid: str, bucket: str='mf-storage-prod'):
    from google.cloud import storage
    from datetime import timedelta

    # TODO - instead of all this credential passing - should set up service specific service account and give correct permissions
    from google.oauth2 import service_account
    sa_creds = get_secret("GCS_SA", "service-account-cred/versions/6")
    service_account_info = json.loads(sa_creds)
    credentials = service_account.Credentials.from_service_account_info(service_account_info)

    storage_client = storage.Client(credentials = credentials)
    bucket = storage_client.bucket(bucket)
    ds_blobs = bucket.list_blobs(prefix = f'{dsid}/')
    urls = {}
    for blob in ds_blobs:
        urls[blob.name] = blob.generate_signed_url(
        version="v4",
        expiration=timedelta(hours=1),
        method="GET")

    return urls


@router.post("/datasets/{dsid}/upload", response_model=AssociatedFileRead)
async def create_dataset_from_file_upload(files: Annotated[List[UploadFile], File(description="List of files")],
                                          auth_res:  Annotated[tuple, Depends(authorize_user)],
                                          dsid: str):
    '''
    Upload files to cloud bucket and add to associated files list for dataset.
    '''

    user_unique_id, ag_list = auth_res
   # ag_ids = [ag.id for ag in ag_list]
    # upload the file
    for file in files:
        dest_dir = f"/mnt/gcs/api-uploads/{os.path.dirname(file.filename)}"

        Path(dest_dir).mkdir(parents=True, exist_ok=True)
        save_file_path = f"/mnt/gcs/api-uploads/{file.filename}"
        with open(save_file_path, "wb") as f:
            f.write(await file.read())

    # create associated files record
    af = AssociatedFileCreate(
        filename=os.path.join("api-uploads", file.filename),
        size=os.path.getsize(save_file_path),
        sha256_hash=checkhash(save_file_path),
    )
    return add_associated_file(auth_res, dsid, af)


@router.post("/datasets/{dsid}/ingest", response_model=IngestionRequest)
def request_dataset_ingestion(auth_res: Annotated[tuple, Depends(authorize_user)],
                              dsid: str,
                              file_to_upload: Optional[str]=None,
                              ingestion_class: Optional[str]=None):

    user_unique_id, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]
    print("request received")
    ds_cmd = select(Dataset).select_from(join(Dataset, ResourceACL, ResourceACL.mfid == Dataset.unique_id)).where(
                                        Dataset.unique_id == dsid).filter(
                                        ResourceACL.accessgroup_id.in_(ag_ids)).distinct()

    with Session(engine) as session:
        ds = session.exec(ds_cmd).one_or_none()

    if ds is None:
        raise HTTPException(status_code=404, detail="Dataset not found")

    elif file_to_upload is None:
        file_to_upload = ds.file_to_upload

    else:
        ds.file_to_upload = file_to_upload

    if not file_to_upload:
        raise HTTPException(status_code=400, detail="No file listed to ingest")

    pst= pytz.timezone("America/Los_Angeles")
    creation_time = get_tz_isoformat()
    ingestion_request = IngestionRequest(dataset_id=dsid,
                                         time_created=creation_time,
                                         file_to_upload=file_to_upload,
                                         status="requested")
    with Session(engine) as session:
        session.add(ingestion_request)
        session.commit()
        session.refresh(ingestion_request)
        print('committed')

    rmq_pw = get_secret("RABBITMQ_DEFAULT_PW", "rabbitmq_default_pw/versions/1")
    msg = {
        "filename": file_to_upload,
        "reqid": ingestion_request.id,
        "dsid": dsid,
        "ingestion_class": ingestion_class
    }

    send_pika_message(
        message=json.dumps(msg),
        exchange="",
        routing_key="ingest-newapi",
        user="admin",
        rmq_pw=rmq_pw
    )

    return ingestion_request


@router.get("/datasets/{dsid}/ingest/{reqid}", response_model=IngestionRequest)
def get_ingestion_request(auth_res: Annotated[tuple, Depends(authorize_user)], reqid: int, dsid: str):
    """
    Get a specific ingestion request by dataset ID and request ID.

    Args:
        auth_res: Authorization result containing access group info
        reqid: Ingestion request ID
        dsid: Dataset ID

    Returns:
        IngestionRequest object if found and user has access

    Raises:
        HTTPException: If request not found or access denied
    """
    user_unique_id, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    # Check dataset access first
    ds_cmd = select(Dataset).select_from(join(Dataset, ResourceACL, ResourceACL.mfid == Dataset.unique_id)).where(
        Dataset.unique_id == dsid).filter(ResourceACL.accessgroup_id.in_(ag_ids)).distinct()

    with Session(engine) as session:
        ds = session.exec(ds_cmd).one_or_none()

        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found or access denied")

        # Get the ingestion request
        ing_req = session.exec(select(IngestionRequest).where(
            (IngestionRequest.id == reqid) & (IngestionRequest.dataset_id == dsid)
        )).one_or_none()

        if ing_req is None:
            raise HTTPException(status_code=404, detail="Ingestion request not found")

        return ing_req


@router.patch("/datasets/{dsid}/ingest/{reqid}", response_model=IngestionRequest)
def patch_ingestion_request(auth_res: Annotated[tuple, Depends(authorize_user)], reqid: int, dsid: str, ing_req: IngestionRequest):
    _, ag_list = auth_res
    require_groups(ag_list)
    if ing_req.id:
        print(f"{reqid=}")
        print(f"{ing_req.id=}")
        assert reqid == ing_req.id

    with Session(engine) as session:
        updated_req = only_update(session,
                                  IngestionRequest,
                                  (IngestionRequest.id == reqid) & (IngestionRequest.dataset_id == dsid),
                                  **ing_req.model_dump(exclude_unset=True))
        if updated_req is None:
            errmsg = "No ingestion request found for the \
                      provided dataset and request ID combination"

            raise HTTPException(status_code=400,
                                detail=errmsg)

        session.commit()
        session.refresh(updated_req)

    return(updated_req)


@router.get("/datasets/{dsid}/scientific_metadata")
def read_scientific_metadata(auth_res: Annotated[tuple, Depends(authorize_user)], dsid: str):
    user_unique_id, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]
    with Session(engine) as session:
        ds_cmd = select(Dataset).select_from(join(Dataset, ResourceACL, ResourceACL.mfid == Dataset.unique_id, isouter=True)).where(Dataset.unique_id == dsid).filter((ResourceACL.accessgroup_id.in_(ag_ids)) | (Dataset.public == True)).distinct()
        ds = session.exec(ds_cmd).one_or_none()

        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found or access denied")

        ds_md = session.exec(select(ScientificMetadata).where(ScientificMetadata.dataset_unique_id == ds.unique_id)).one_or_none()
        return ds_md


@router.post("/datasets/{dsid}/scientific_metadata", response_model=Union[ScientificMetadata, NewScientificMetadata])
def add_scientific_metadata(auth_res: Annotated[tuple, Depends(authorize_user)], dsid: str, scientific_md: dict):
    user_unique_id, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]
    ds_cmd = select(Dataset).select_from(join(Dataset,
                                              ResourceACL,
                                              ResourceACL.mfid == Dataset.unique_id)).where(
                                                  Dataset.unique_id == dsid).filter(
                                                        ResourceACL.accessgroup_id.in_(ag_ids)).distinct()

    with Session(engine) as session:
        ds = session.exec(ds_cmd).one_or_none()

        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found")
        
        md_obj = add_or_update(
            session,
            ScientificMetadata,
            where=(ScientificMetadata.dataset_unique_id == dsid),
            dataset_unique_id = dsid,
            scientific_metadata = scientific_md)

        attributes.flag_modified(md_obj, 'scientific_metadata')

        new_md_obj = add_or_update(
            session,
            NewScientificMetadata,
            where=(NewScientificMetadata.dataset_mfid == dsid),
            dataset_mfid=dsid,
            dataset_id=ds.id,
            scientific_metadata=scientific_md,
        )

        attributes.flag_modified(new_md_obj, 'scientific_metadata')

        ri = session.exec(select(Resource).where(Resource.mfid == dsid)).one_or_none()
        if ri:
            ri.modification_time = get_tz_isoformat()

        # returns the old formatted md obj
        session.commit()
        session.refresh(md_obj)
        return(md_obj)


@router.patch("/datasets/{dsid}/scientific_metadata", response_model=Union[ScientificMetadata, NewScientificMetadata])
def update_scientific_metadata(auth_res: Annotated[tuple, Depends(authorize_user)], dsid: str, scientific_md_updates: dict):
    user_unique_id, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]
    ds_cmd = select(Dataset).select_from(join(Dataset, ResourceACL, ResourceACL.mfid == Dataset.unique_id)).where(Dataset.unique_id == dsid).filter(ResourceACL.accessgroup_id.in_(ag_ids)).distinct()

    with Session(engine) as session:
        ds = session.exec(ds_cmd).one_or_none()

        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found")

        stored_md = session.exec(select(ScientificMetadata).where(ScientificMetadata.dataset_unique_id == dsid)).one_or_none()
        print(f'{stored_md=}')
        if stored_md:
            md_dict = stored_md.scientific_metadata

        else:
            md_dict = {}

        print(f'{md_dict=}')

        # this doesn't allow for updating nested fields without providing the whole tree 
        # from the first level down.
        # could make recursive?
        for k,v in scientific_md_updates.items():
            md_dict[k] = v
        print(f'after update: {md_dict=}')
        md_obj = add_or_update(
            session,
            ScientificMetadata,
            where=(ScientificMetadata.dataset_unique_id == dsid),
            dataset_unique_id = dsid,
            scientific_metadata = md_dict)
        attributes.flag_modified(md_obj, 'scientific_metadata')

        new_md_obj = add_or_update(
            session,
            NewScientificMetadata,
            where=(NewScientificMetadata.dataset_mfid == dsid),
            dataset_mfid=dsid,
            dataset_id=ds.id,
            scientific_metadata=md_dict,
        )

        attributes.flag_modified(new_md_obj, 'scientific_metadata')
        ri = session.exec(select(Resource).where(Resource.mfid == dsid)).one_or_none()
        if ri:
            ri.modification_time = get_tz_isoformat()

        session.commit()
        session.refresh(md_obj)
        return(md_obj)


@router.get("/datasets/{dsid}/thumbnails", response_model=List[ThumbnailRead])
def read_thumbnails(auth_res: Annotated[tuple, Depends(authorize_user)], dsid: str):
    '''
    Returns a list of thumbnail objects associated with the dataset ID provided
    '''
    user_unique_id, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]
    with Session(engine) as session:
        ds_cmd = select(Dataset).select_from(join(Dataset, ResourceACL, ResourceACL.mfid == Dataset.unique_id, isouter=True)).where(Dataset.unique_id == dsid).filter((ResourceACL.accessgroup_id.in_(ag_ids)) | (Dataset.public == True)).distinct()
        ds = session.exec(ds_cmd).one_or_none()

        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found or access denied")

        ds_thumbnails = session.exec(select(Thumbnail).where(Thumbnail.dataset_id == ds.id)).all()
        return ds_thumbnails

@router.post("/datasets/first_thumbnails")
def get_first_thumbnails(
    auth_res: Annotated[tuple, Depends(authorize_user)],
    dataset_ids: List[str] = Body(...),
):
    '''
    Given a list of dataset unique_ids, returns a dict mapping each unique_id
    to its first (lowest-id) thumbnail. Datasets with no thumbnail or that the
    caller cannot access are omitted from the response.

    Intended for bulk thumbnail fetching after an entity_graph call.
    '''
    _, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        auth_filter = (ResourceACL.accessgroup_id.in_(ag_ids)) | (Dataset.public == True)

        authorized_ds = session.exec(
            select(Dataset)
            .join(ResourceACL, ResourceACL.mfid == Dataset.unique_id, isouter=True)
            .filter(auth_filter)
            .where(Dataset.unique_id.in_(dataset_ids))
            .distinct()
        ).all()

        ds_id_to_uid = {ds.id: ds.unique_id for ds in authorized_ds}

        if not ds_id_to_uid:
            return {}

        # Subquery: minimum (first) thumbnail id per dataset
        min_thumb_subq = (
            select(Thumbnail.dataset_id, func.min(Thumbnail.id).label('min_id'))
            .where(Thumbnail.dataset_id.in_(list(ds_id_to_uid.keys())))
            .group_by(Thumbnail.dataset_id)
            .subquery()
        )

        first_thumbnails = session.exec(
            select(Thumbnail).join(
                min_thumb_subq,
                and_(
                    Thumbnail.dataset_id == min_thumb_subq.c.dataset_id,
                    Thumbnail.id == min_thumb_subq.c.min_id,
                )
            )
        ).all()

        return {
            ds_id_to_uid[t.dataset_id]: {
                "thumbnail_name": t.thumbnail_name,
                "thumbnail_b64str": t.thumbnail_b64str,
            }
            for t in first_thumbnails
        }
    
@router.post("/datasets/{dsid}/thumbnails", response_model=ThumbnailRead)
def add_thumbnail(auth_res: Annotated[tuple, Depends(authorize_user)],
                  dsid: str,
                  thumbnail: ThumbnailCreate):
    '''
    Adds a thumbnail to a dataset. If the thumbnail exists, it will simply add the existing thumbnail to the dataset object.
    Otherwise, the thumbnail will be created and then added to the dataset.

    Raises:
        HTTPException: If required thumbnail fields are missing or dataset not found
    '''
    user_unique_id, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    # Validate required fields
    missing_fields = []
    if not thumbnail.thumbnail_name or not thumbnail.thumbnail_name.strip():
        missing_fields.append("thumbnail_name")
    if not thumbnail.thumbnail_b64str or not thumbnail.thumbnail_b64str.strip():
        missing_fields.append("thumbnail_b64str")

    if missing_fields:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required thumbnail information: {', '.join(missing_fields)}. Please provide all required fields: thumbnail_name and thumbnail_b64str."
        )

    ds_cmd = select(Dataset).select_from(join(Dataset, ResourceACL, ResourceACL.mfid == Dataset.unique_id)).where(Dataset.unique_id == dsid).filter(ResourceACL.accessgroup_id.in_(ag_ids)).distinct()

    with Session(engine) as session:
        ds = session.exec(ds_cmd).one_or_none()

        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found")

        tn_obj = add_or_update(session, Thumbnail, where=(Thumbnail.thumbnail_b64str == thumbnail.thumbnail_b64str),
                                thumbnail_name = thumbnail.thumbnail_name,
                                thumbnail_b64str = thumbnail.thumbnail_b64str,
                                dataset_id = ds.id)
        print('added tnobj')
        if not (tn_obj in ds.thumbnails):
            print("adding tnobj to ds")
            ds.thumbnails.append(tn_obj)
            session.add(ds)
            print("added tnobj to ds")

        ri = session.exec(select(Resource).where(Resource.mfid == dsid)).one_or_none()
        if ri:
            ri.modification_time = get_tz_isoformat()

        session.commit()
        print("committed session")
        session.refresh(tn_obj)

    return(tn_obj)


@router.get("/datasets/{dsid}/associated_files", response_model=List[AssociatedFileRead])
def read_associated_files(auth_res: Annotated[tuple, Depends(authorize_user)], dsid: str):
    '''
    Returns a list of the files associated with the dataset ID provided.
    '''
    user_unique_id, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        ds_cmd = select(Dataset).select_from(join(Dataset, ResourceACL, ResourceACL.mfid == Dataset.unique_id, isouter=True)).where(Dataset.unique_id == dsid).filter((ResourceACL.accessgroup_id.in_(ag_ids)) | (Dataset.public == True)).distinct()
        ds = session.exec(ds_cmd).one_or_none()

        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found or access denied")

        ds_files = session.exec(select(AssociatedFile).where(AssociatedFile.dataset_id == ds.id)).all()
        return ds_files


@router.post("/datasets/{dsid}/associated_files", response_model=AssociatedFileRead)
def add_associated_file(auth_res: Annotated[tuple, Depends(authorize_user)],
                        dsid: str,
                        associated_file: AssociatedFileCreate):
    '''
    Adds an associated file to a dataset.
    Creates the AF object if it doesn't already exist.

    Raises:
        HTTPException: If required associated file fields are missing or dataset not found
    '''
    print("accepted args")
    user_unique_id, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    # Validate required fields
    missing_fields = []
    if not associated_file.filename or not associated_file.filename.strip():
        missing_fields.append("filename")
    if associated_file.size is None:
        missing_fields.append("size")
    if not associated_file.sha256_hash or not associated_file.sha256_hash.strip():
        missing_fields.append("sha256_hash")

    if missing_fields:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required associated file information: {', '.join(missing_fields)}. Please provide all required fields: filename, size, and sha256_hash."
        )

    ds_cmd = select(Dataset).select_from(join(Dataset, ResourceACL, ResourceACL.mfid == Dataset.unique_id)).where(Dataset.unique_id == dsid).filter(ResourceACL.accessgroup_id.in_(ag_ids)).distinct()

    with Session(engine) as session:
        ds = session.exec(ds_cmd).one_or_none()

        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found")

        af_obj = add_or_update(session, AssociatedFile, where=(AssociatedFile.sha256_hash == associated_file.sha256_hash),
                                       filename = associated_file.filename,
                                       size = associated_file.size,
                                       sha256_hash = associated_file.sha256_hash,
                                       dataset_id = ds.id)
        print("added af_obj")
        if not (af_obj in ds.associated_files):
            print("adding afobj to ds")
            ds.associated_files.append(af_obj)
            session.add(ds)
            print("added afobj to ds")

        ri = session.exec(select(Resource).where(Resource.mfid == dsid)).one_or_none()
        if ri:
            ri.modification_time = get_tz_isoformat()

        session.commit()
        print("committed session")
        session.refresh(af_obj)
        return(af_obj)


@router.get("/datasets/{dsid}/keywords", response_model=List[Keyword])
def list_keywords_for_dataset(auth_res: Annotated[tuple, Depends(authorize_user)], dsid: str):

    user_unique_id, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        ds_cmd = select(Dataset).select_from(join(Dataset, ResourceACL, ResourceACL.mfid == Dataset.unique_id, isouter=True)).where(Dataset.unique_id == dsid).filter((ResourceACL.accessgroup_id.in_(ag_ids)) | (Dataset.public == True)).distinct()
        ds = session.exec(ds_cmd).one_or_none()

        if ds is None:
            raise HTTPException(status_code=403, detail="Dataset not found or access denied")

        ds_keywords = session.exec(select(Keyword).select_from(join(Keyword, DatasetKeywordLink, DatasetKeywordLink.keyword_id == Keyword.id)).where(DatasetKeywordLink.dataset_id == ds.id)).all()
        return ds_keywords


@router.post("/datasets/{dsid}/keywords", response_model=Keyword)
def add_keyword(auth_res: Annotated[tuple, Depends(authorize_user)], dsid: str, keyword: str):
    '''
    Adds a keyword to a dataset. If the keyword exists, it will simply add the existing keyword to the dataset object.
    Otherwise, the keyword will be created and then added to the dataset.
    '''
    user_unique_id, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]
    ds_cmd = select(Dataset).select_from(join(Dataset, ResourceACL, ResourceACL.mfid == Dataset.unique_id)).where(Dataset.unique_id == dsid).filter(ResourceACL.accessgroup_id.in_(ag_ids)).distinct()

    with Session(engine) as session:
        kw_obj = add_or_update(session, Keyword, where=(Keyword.keyword == keyword), keyword=keyword)

        ds = session.exec(ds_cmd).one_or_none()

        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found")

        if not (kw_obj in ds.keywords):
            ds.keywords.append(kw_obj)

        count = session.exec(select(func.count(DatasetKeywordLink.dataset_id))\
                            .where(DatasetKeywordLink.keyword_id == kw_obj.id)).one()

        kw_obj.num_datasets = count
        session.add(kw_obj)
        session.add(ds)
        session.commit()
        session.refresh(kw_obj)
        return(kw_obj)


@router.get("/datasets/{dsid}/access_groups", response_model=List[AccessGroupRead])
def read_dataset_acl(dsid: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    _, ag_list = auth_res
    require_groups(ag_list)
    with Session(engine) as session:
        # Verify dataset exists
        ds = session.exec(select(Dataset).where(Dataset.unique_id == dsid)).one_or_none()

        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found")

        dscmd = select(AccessGroup).select_from(
            join(AccessGroup,
                 join(ResourceACL, Dataset, ResourceACL.mfid == Dataset.unique_id),
                 AccessGroup.id == ResourceACL.accessgroup_id)
        ).where(Dataset.unique_id == dsid)
        ds_acl = session.exec(dscmd).all()

        return ds_acl


@router.post("/datasets/{dsid}/access_groups", response_model=ResourceACL)
def create_dataset_acl(group_name: str, dsid: str, read: bool, write: bool, auth_res: Annotated[tuple, Depends(authorize_user)]):
    _, ag_list = auth_res
    require_groups(ag_list)
    with Session(engine) as session:
        print(f"{dsid=}")
        ds = session.exec(select(Dataset).where(Dataset.unique_id == dsid)).one_or_none()
        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found")
        ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == group_name)).one_or_none()
        if ag is None:
            raise HTTPException(status_code=404, detail = "Access Group not found")
        acl = sql_create_resource_acl(ag, dsid, read, write, session)
    return acl


@router.post("/datasets/{dsid}/samples/{sample_id}", response_model=DatasetSampleLinkResponse)
def add_sample_to_dataset(auth_res: Annotated[tuple, Depends(authorize_user)], dsid: str, sample_id: str):
    user_unique_id, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]
    ds_cmd = select(Dataset).select_from(join(Dataset, ResourceACL, ResourceACL.mfid == Dataset.unique_id)).where(Dataset.unique_id == dsid).filter(ResourceACL.accessgroup_id.in_(ag_ids)).distinct()
    samp_cmd = select(Sample).where(Sample.unique_id == sample_id)

    with Session(engine) as session:
        ds = session.exec(ds_cmd).one_or_none()
        samp = session.exec(samp_cmd).one_or_none()

        if samp is None:
            raise HTTPException(status_code=404, detail="Sample not found")
        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found or access denied")


        add_or_update(session,
                      DatasetSampleLink,
                      where=(and_(DatasetSampleLink.sample_id == samp.id, DatasetSampleLink.dataset_id == ds.id)),
                      sample_id = samp.id,
                      dataset_id = ds.id
                      )

        session.commit()

        info = DatasetSampleLinkResponse(dataset_id = ds.unique_id, dataset_name = ds.dataset_name, sample_id = samp.unique_id, sample_type = samp.sample_type, sample_name = samp.sample_name)
    return info


@router.get("/datasets/{dsid}/samples", response_model=List[SampleRead])
def read_samples_in_dataset(dsid: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    '''
    Returns a list of samples associated with the dataset ID provided
    '''
    user_unique_id, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]
    with Session(engine) as session:
        ds_cmd = select(Dataset).select_from(join(Dataset, ResourceACL, ResourceACL.mfid == Dataset.unique_id, isouter=True)).where(Dataset.unique_id == dsid).filter((ResourceACL.accessgroup_id.in_(ag_ids)) | (Dataset.public == True)).distinct()
        ds = session.exec(ds_cmd).one_or_none()

        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found or access denied")

        samples = session.exec(select(Sample).select_from(join(Sample, DatasetSampleLink, DatasetSampleLink.sample_id == Sample.id)).where(DatasetSampleLink.dataset_id == ds.id)).all()
        return samples

@router.get("/datasets/{dataset_id}/dataset_graph")
def dataset_graph(dataset_id: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    '''
    Returns a NetworkX node-link graph of all datasets connected (via parent/child
    relationships) to the given dataset. Loads all authorized datasets in the
    project and returns the directed lineage subgraph (ancestors + descendants) of the target.
    '''
    _, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        auth_filter = (ResourceACL.accessgroup_id.in_(ag_ids)) | (Dataset.public == True)

        target = session.exec(
            select(Dataset)
            .join(ResourceACL, ResourceACL.mfid == Dataset.unique_id, isouter=True)
            .filter(auth_filter)
            .where(Dataset.unique_id == dataset_id)
        ).first()

        if target is None:
            raise HTTPException(status_code=404, detail="Dataset not found")

        ds_list = session.exec(
            select(Dataset)
            .options(joinedload(Dataset.parents))
            .join(ResourceACL, ResourceACL.mfid == Dataset.unique_id, isouter=True)
            .filter(auth_filter)
            .where(Dataset.project_id == target.project_id)
        ).unique().all()

        edges = []
        for dataset in ds_list:
            for parent in dataset.parents:
                edges.append((parent.unique_id, dataset.unique_id))

        G = nx.DiGraph(edges)
        for dataset in ds_list:
            G.add_node(dataset.unique_id, dataset_name=dataset.dataset_name)

        if target.unique_id not in G:
            G.add_node(target.unique_id, dataset_name=target.dataset_name)

        # Use directed ancestors + descendants rather than the weakly connected
        # component. WCC would include siblings (nodes that share a parent with
        # the target but are not in its direct lineage). nx.ancestors/descendants
        # traverses strictly along the target's own lineage chain in both
        # directions, excluding siblings.
        component_nodes = nx.ancestors(G, target.unique_id) | nx.descendants(G, target.unique_id) | {target.unique_id}
        return json_graph.node_link_data(G.subgraph(component_nodes))


@router.get("/datasets/{dataset_id}/dataset_graph_cte")
def dataset_graph_cte(dataset_id: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    '''
    CTE-based implementation (kept for reference). Prefer /dataset_graph.
    Returns a NetworkX node-link graph of all datasets connected (via parent/child
    relationships) to the given dataset, i.e. its full lineage subgraph.
    '''
    _, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        auth_filter = (ResourceACL.accessgroup_id.in_(ag_ids)) | (Dataset.public == True)

        target = session.exec(
            select(Dataset)
            .join(ResourceACL, ResourceACL.mfid == Dataset.unique_id, isouter=True)
            .filter(auth_filter)
            .where(Dataset.unique_id == dataset_id)
        ).first()

        if target is None:
            raise HTTPException(status_code=404, detail="Dataset not found")

        # Two recursive CTEs — ancestors (walk up) and descendants (walk down)
        cte_sql = text("""
            WITH RECURSIVE
            ancestors(id, unique_id, dataset_name) AS (
                SELECT id, unique_id, dataset_name
                FROM dataset WHERE unique_id = :dataset_uid
              UNION
                SELECT d.id, d.unique_id, d.dataset_name
                FROM dataset d
                JOIN datasetlink dl ON dl.parent_dataset_id = d.id
                JOIN ancestors a ON dl.child_dataset_id = a.id
            ),
            descendants(id, unique_id, dataset_name) AS (
                SELECT id, unique_id, dataset_name
                FROM dataset WHERE unique_id = :dataset_uid
              UNION
                SELECT d.id, d.unique_id, d.dataset_name
                FROM dataset d
                JOIN datasetlink dl ON dl.child_dataset_id = d.id
                JOIN descendants desc_ ON dl.parent_dataset_id = desc_.id
            )
            SELECT DISTINCT id, unique_id, dataset_name
            FROM (SELECT * FROM ancestors UNION ALL SELECT * FROM descendants) combined
        """)
        rows = session.execute(cte_sql, {"dataset_uid": dataset_id}).all()

        id_to_uid = {r.id: r.unique_id for r in rows}
        node_ids = list(id_to_uid.keys())

        # Fetch all edges whose both endpoints are within the component
        edge_rows = session.exec(
            select(DatasetLink.parent_dataset_id, DatasetLink.child_dataset_id)
            .where(DatasetLink.parent_dataset_id.in_(node_ids))
            .where(DatasetLink.child_dataset_id.in_(node_ids))
        ).all()

        G = nx.DiGraph((id_to_uid[r[0]], id_to_uid[r[1]]) for r in edge_rows)
        for r in rows:
            G.add_node(r.unique_id, dataset_name=r.dataset_name)

        return json_graph.node_link_data(G)


import json
import logging
import os
from typing import Annotated, List, Literal, Optional

logger = logging.getLogger(__name__)

import networkx as nx
from networkx.readwrite import json_graph
from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Query, Request
from sqlalchemy import exists, func, join, literal, text
from sqlalchemy.orm import joinedload, selectinload
from sqlmodel import and_, or_, Session, select

from app.api.dependencies import get_engine, get_trgm_search_threshold
from app.general_utils import get_secret, get_utc_isoformat
from app.models.core_models import (
    AccessGroup,
    AssociatedFile,
    Dataset,
    IngestionRequest,
    ResourceACL,
    DatasetKeywordLink,
    DatasetLink,
    DatasetSampleLink,
    DeletionRequest,
    Instrument,
    Keyword,
    Project,
    Resource,
    Sample,
    ScientificMetadataBinary,
    Thumbnail,
    User,
)
from app.models.response_schemas import DatasetSampleLinkResponse
from app.models.datasets import (
    AssociatedFileCreate, AssociatedFileRead,
    DatasetCreate, DatasetRead, DatasetResponse, DatasetUpdate,
    ThumbnailCreate, ThumbnailRead,
)
from app.models.links import LinkedResource
from app.models.pagination import PaginatedResponse
from app.models.samples import SampleRead
from app.models.users import UserPublicRead
from app.utils.auth_utils import authorize_user, require_permission, permission_filter
from app.utils.permissions import Permission, Principal, Role
from app.utils.project_utils import project_standing, resolve_project_id_to_mfid, resolve_project_mfids_to_ids
from app.utils.instrument_utils import resolve_instrument_mfids_to_ids
from app.utils.rmq_utils import send_pika_message, get_ingestion_tier
from app.utils.user_utils import resolve_user_by_identifier
from app.utils.sql_utils import add_or_update, only_update, generate_kw_query, sql_create_resource_acl, create_standard_acls, get_active_deletion_request, touch_resource_modification_time, set_public, deletion_filter_for, visibility_filter_for
from app.utils.gcs_record_utils import write_record, dataset_record
from app.api.routes.gcs_storage import _ensure_storage_path, _generate_signed_url_link

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

API_VERSION_GITHASH = os.getenv("GITHASH", None)
RMQ_ROUTING_SUFFIX = os.getenv("RMQ_ROUTING_SUFFIX", None)

@router.post("/datasets/{dsid}/carrier_segmentation")
def request_carrier_segmentation(dsid, auth_res: Annotated[Principal, Depends(authorize_user)]):
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
def request_insitu_aggregation(dsid, auth_res: Annotated[Principal, Depends(authorize_user)]):
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

@router.post("/datasets/{dsid}/rga_analysis")
def request_rga_analysis(dsid, auth_res: Annotated[Principal, Depends(authorize_user)]):
    with Session(engine) as session:
        dataset = session.exec(select(Dataset).where(Dataset.unique_id == dsid)).one_or_none()
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found")

    rmq_pw = get_secret("RABBITMQ_DEFAULT_PW", "rabbitmq_default_pw/versions/1")
    msg = {
        "dsid": dataset.unique_id,
    }

    send_pika_message(
        message=json.dumps(msg),
        exchange="",
        routing_key="rga-analysis",
        user="admin",
        rmq_pw=rmq_pw
    )
    return

@router.post("/datasets/{dsid}/mosaic_stitch")
def request_mosaic_stitch(dsid, auth_res: Annotated[Principal, Depends(authorize_user)]):
    with Session(engine) as session:
        dataset = session.exec(select(Dataset).where(Dataset.unique_id == dsid)).one_or_none()
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found")

    rmq_pw = get_secret("RABBITMQ_DEFAULT_PW", "rabbitmq_default_pw/versions/1")
    msg = {
        "dsid": dataset.unique_id,
    }

    send_pika_message(
        message=json.dumps(msg),
        exchange="",
        routing_key="mosaic-stitch",
        user="admin",
        rmq_pw=rmq_pw
    )
    return

@router.post("/datasets", response_model=DatasetResponse)
async def create_dataset(auth_res: Annotated[Principal, Depends(authorize_user)],
                         dataset: DatasetCreate,
                         background_tasks: BackgroundTasks):
    
    logger.info(f"Creating dataset: {dataset.unique_id}")
    dataset_details = dataset.model_dump(exclude_unset=True)
    user_unique_id = auth_res.user_id
    with Session(engine) as session:
        if dataset_details.get('unique_id'):
            existing = session.exec(select(Dataset).where(Dataset.unique_id == dataset_details['unique_id'])).one_or_none()
            if existing is not None:
                raise HTTPException(status_code=409, detail=f"Dataset '{dataset_details['unique_id']}' already exists. Use PATCH to update it.")

        owner_identifier = dataset_details.pop('owner', None)
        if owner_identifier:
            if dataset.owner_orcid:
                raise HTTPException(status_code=400, detail="Provide owner or owner_orcid, not both")
            dataset_details['owner_orcid'] = resolve_user_by_identifier(session, owner_identifier).unique_id

        if not dataset_details.get('owner_orcid'):
            dataset_details['owner_orcid'] = user_unique_id

        if not auth_res.is_platform_admin:
            owner_orcid = dataset_details['owner_orcid']
            proj_id = dataset_details.get('project_id')
            if proj_id:
                standing = project_standing(owner_orcid, proj_id, session)
                if standing is None or standing < Role.CONTRIBUTOR:
                    raise HTTPException(
                        status_code=403,
                        detail=f"'{owner_orcid}' must be contributor or above in project '{proj_id}' to create there")
            elif owner_orcid != user_unique_id:
                if not auth_res.is_service_account:
                    raise HTTPException(
                        status_code=400,
                        detail="Creating on behalf of another user requires project_id, to verify their standing there")
                if session.exec(select(User).where(User.unique_id == owner_orcid)).one_or_none() is None:
                    raise HTTPException(status_code=404, detail=f"Owner '{owner_orcid}' not found")

        if dataset_details.get('owner_orcid', None) is not None:
            user = session.exec(select(User).where(User.unique_id == dataset_details['owner_orcid'])).one_or_none()
            dataset_details['owner_user_id'] = user.id if user else None

        if dataset_details.get('project_id', None):
            mfid = resolve_project_id_to_mfid(dataset_details['project_id'], session)
            if mfid is None:
                raise HTTPException(status_code=404, detail=f"Project '{dataset_details['project_id']}' not found")
            dataset_details['project_mfid'] = mfid

        instrument = None
        instrument_id_slug = dataset_details.pop('instrument_id', None)
        if instrument_id_slug:
            instrument = session.exec(select(Instrument).where(Instrument.instrument_id == instrument_id_slug)).one_or_none()
            if instrument is None:
                raise HTTPException(status_code=400, detail=f"Instrument '{instrument_id_slug}' is not registered")
            dataset_details['instrument_name'] = instrument.instrument_name
        elif dataset_details.get('instrument_name', None):
            instrument = session.exec(
                select(Instrument).where(func.lower(Instrument.instrument_name) == dataset_details['instrument_name'].lower())
            ).one_or_none()
            if instrument is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"Instrument '{dataset_details['instrument_name']}' is not registered")
        if instrument is not None:
            dataset_details['instrument_id'] = instrument.id
            dataset_details['instrument_mfid'] = instrument.unique_id

        ds = Dataset(**dataset_details)
        session.add(ds)
        session.flush()

        # get time
        now = get_utc_isoformat()
        
        # fetch resource
        existing_ri = session.exec(select(Resource).where(Resource.mfid == ds.unique_id)).one_or_none()
        ri = add_or_update(session, Resource, where=(Resource.mfid == ds.unique_id),
                           mfid = ds.unique_id, resource_type = "dataset")

        # populate resource table
        if existing_ri is None:
            ri.creation_time = now
        ri.modification_time = now
        ri.created_with_api_version = API_VERSION
        ri.api_version_githash = API_VERSION_GITHASH
        ri.public = ds.public
        set_public(session, ds.unique_id, bool(ds.public))
        
        session.flush()

        create_standard_acls(session, ds.unique_id,
                             owner_id=dataset_details.get('owner_orcid'),
                             requestor_id=user_unique_id,
                             project_id=dataset_details.get('project_id'))

        if instrument is not None:
            inst_ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == instrument.unique_id)).one_or_none()
            if inst_ag is None:
                raise HTTPException(
                    status_code=500,
                    detail=f"Instrument '{instrument.instrument_name}' has no operator group provisioned")
            sql_create_resource_acl(inst_ag, ds.unique_id, read=True, write=True,
                                    session=session, permission=Role.CONTRIBUTOR)

        session.commit()
        session.refresh(ds)
        background_tasks.add_task(write_record, ds.unique_id, dataset_record(ds, ri), user_unique_id, "create")
        return DatasetResponse.model_validate({
            **ds.model_dump(),
            "instrument_id": instrument.instrument_id if instrument else None,
            "public": ri.public,
            "creation_time": ri.creation_time,
            "modification_time": ri.modification_time,
        })


@router.patch("/datasets/{dsid}", response_model=DatasetResponse)
def update_dataset(auth_res: Annotated[Principal, Depends(authorize_user)], dsid: str, updated_dataset: DatasetUpdate,
                   background_tasks: BackgroundTasks):
    '''
    Partially update a dataset record. Only provided fields are modified.
    No owner_orcid/project_id — use POST /resources/{mfid}/transfer_ownership
    and POST /resources/{mfid}/project.
    '''
    user_unique_id = auth_res.user_id

    with Session(engine) as session:
        ds = session.exec(select(Dataset).where(Dataset.unique_id == dsid)).one_or_none()
        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found")

        require_permission(session, auth_res, dsid, Permission.EDIT)

        update_dict = updated_dataset.model_dump(exclude_unset=True)
        instrument = None
        instrument_id_slug = update_dict.pop('instrument_id', None)
        if instrument_id_slug:
            instrument = session.exec(select(Instrument).where(Instrument.instrument_id == instrument_id_slug)).one_or_none()
            if instrument is None:
                raise HTTPException(status_code=400, detail=f"Instrument '{instrument_id_slug}' is not registered")
            update_dict['instrument_name'] = instrument.instrument_name
        elif update_dict.get('instrument_name', None):
            instrument = session.exec(
                select(Instrument).where(func.lower(Instrument.instrument_name) == update_dict['instrument_name'].lower())
            ).one_or_none()
            if instrument is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"Instrument '{update_dict['instrument_name']}' is not registered")
        if instrument is not None:
            update_dict['instrument_id'] = instrument.id
            update_dict['instrument_mfid'] = instrument.unique_id

        upds = add_or_update(session, Dataset, Dataset.unique_id == dsid, **update_dict)

        ri = session.exec(select(Resource).where(Resource.mfid == dsid)).one_or_none()
        if ri:
            ri.modification_time = get_utc_isoformat()
            ri.public = upds.public
            set_public(session, upds.unique_id, bool(upds.public))

        if instrument is not None:
            inst_ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == instrument.unique_id)).one_or_none()
            if inst_ag is None:
                raise HTTPException(
                    status_code=500,
                    detail=f"Instrument '{instrument.instrument_name}' has no operator group provisioned")
            sql_create_resource_acl(inst_ag, upds.unique_id, read=True, write=True,
                                    session=session, permission=Role.CONTRIBUTOR)

        session.commit()
        session.refresh(upds)
        background_tasks.add_task(write_record, upds.unique_id, dataset_record(upds, ri), user_unique_id, "update")
        instrument_ids = resolve_instrument_mfids_to_ids([upds.instrument_mfid], session)
        project_ids = resolve_project_mfids_to_ids([upds.project_mfid], session)
        return DatasetResponse.model_validate({
            **upds.model_dump(),
            "instrument_id": instrument_ids.get(upds.instrument_mfid),
            "project_id": project_ids.get(upds.project_mfid, upds.project_id),
            "public": ri.public if ri else upds.public,
            "creation_time": ri.creation_time if ri else None,
            "modification_time": ri.modification_time if ri else None,
        })


@router.get("/datasets", response_model=PaginatedResponse[DatasetResponse])
def list_datasets(auth_res: Annotated[Principal, Depends(authorize_user)], request: Request,
                  include_metadata: bool = False,
                  include_links: bool = False,
                  include_owner: bool = False,
                  limit: int = Query(default=100, ge=1, le=1000),
                  cursor: Optional[str] = None,
                  offset: int = Query(default=0, ge=0),
                  creation_time_gte: Optional[str] = None, creation_time_lte: Optional[str] = None,
                  modification_time_gte: Optional[str] = None, modification_time_lte: Optional[str] = None,
                  visibility: Literal["all", "public", "private"] = "all"):
    '''
    Returns a paginated list of datasets the requesting user is authorized to view.
    Default limit is 100, max is 1000.

    Keyset (cursor) pagination: results are ordered by unique_id descending, which is
    creation order since unique_id is a time-sortable UUIDv7. To fetch the next page,
    pass cursor=<next_cursor from the previous response>. The first page omits cursor.
    next_cursor is null when there are no further pages. total is included only on the
    first page (cursor omitted); subsequent pages omit it to avoid a redundant count.

    Offset pagination is supported for backward compatibility: pass offset=<n> instead
    of cursor. cursor takes precedence when both are given. Prefer cursor for large
    result sets, since deep offsets are slower.
    Pass include_links=true to include parent, child, and associated resources for each dataset.
    Pass visibility=public or visibility=private to narrow by the resource's public ACL entry.
    '''
    params_dict = dict(request.query_params)
    keyword_filter = params_dict.pop('keyword', None)

    # extract parameters from filters
    for k in ('creation_time_gte', 'creation_time_lte', 'modification_time_gte',
              'modification_time_lte', 'include_metadata', 'include_links', 'include_owner',
              'limit', 'cursor', 'offset', 'visibility'):
        params_dict.pop(k, None)

    with Session(engine) as session:
        auth_filter = permission_filter(auth_res, Dataset)
        deletion_filter = deletion_filter_for(Dataset)

        # Base filters shared between count and data queries
        def apply_filters(q):
            q = q.where(auth_filter).where(deletion_filter)
            if visibility != "all":
                q = q.where(visibility_filter_for(Dataset, visibility))
            if creation_time_gte:
                q = q.where(Resource.creation_time >= creation_time_gte)
            if creation_time_lte:
                q = q.where(Resource.creation_time <= creation_time_lte)
            if modification_time_gte:
                q = q.where(Resource.modification_time >= modification_time_gte)
            if modification_time_lte:
                q = q.where(Resource.modification_time <= modification_time_lte)
            for key, value in params_dict.items():
                if hasattr(Dataset, key):
                    q = q.where(getattr(Dataset, key) == value)
            return q

        # total is only needed on the first page; cursor pages and deep offset pages
        # skip the redundant count.
        total = None
        if cursor is None and offset == 0:
            count_query = apply_filters(
                select(func.count()).select_from(Dataset)
                .join(Resource, Resource.mfid == Dataset.unique_id, isouter=True)
            )
            if keyword_filter:
                count_query = generate_kw_query(session, count_query, keyword_filter)
            total = session.exec(count_query).one()

        if include_metadata:
            query = (
                select(Dataset, Resource.creation_time, Resource.modification_time,
                       Resource.public, ScientificMetadataBinary.scientific_metadata,
                       Project.project_id, Instrument.instrument_id)
                .join(Resource, Resource.mfid == Dataset.unique_id, isouter=True)
                .join(ScientificMetadataBinary, ScientificMetadataBinary.unique_id == Dataset.unique_id, isouter=True)
                .outerjoin(Project, Project.unique_id == Dataset.project_mfid)
                .outerjoin(Instrument, Instrument.unique_id == Dataset.instrument_mfid)
            )
        else:
            query = (
                select(Dataset, Resource.creation_time, Resource.modification_time, Resource.public,
                       Project.project_id, Instrument.instrument_id)
                .join(Resource, Resource.mfid == Dataset.unique_id, isouter=True)
                .outerjoin(Project, Project.unique_id == Dataset.project_mfid)
                .outerjoin(Instrument, Instrument.unique_id == Dataset.instrument_mfid)
            )

        query = apply_filters(query)
        if keyword_filter:
            query = generate_kw_query(session, query, keyword_filter)
        if cursor is not None:
            query = query.where(Dataset.unique_id < cursor)
        query = query.order_by(Dataset.unique_id.desc())
        if cursor is None and offset:
            query = query.offset(offset)
        query = query.limit(limit)

        results = session.exec(query).all()

        if include_links:
            dataset_ids = [row[0].id for row in results]
            parents_by_dataset: dict = {}
            children_by_dataset: dict = {}
            samples_by_dataset: dict = {}
            if dataset_ids:
                for child_id, parent in session.exec(
                    select(DatasetLink.child_dataset_id, Dataset)
                    .join(Dataset, Dataset.id == DatasetLink.parent_dataset_id)
                    .where(DatasetLink.child_dataset_id.in_(dataset_ids))
                ).all():
                    parents_by_dataset.setdefault(child_id, []).append(
                        LinkedResource(unique_id=parent.unique_id, resource_type="dataset", name=parent.dataset_name, relationship="parent")
                    )
                for parent_id, child in session.exec(
                    select(DatasetLink.parent_dataset_id, Dataset)
                    .join(Dataset, Dataset.id == DatasetLink.child_dataset_id)
                    .where(DatasetLink.parent_dataset_id.in_(dataset_ids))
                ).all():
                    children_by_dataset.setdefault(parent_id, []).append(
                        LinkedResource(unique_id=child.unique_id, resource_type="dataset", name=child.dataset_name, relationship="child")
                    )
                for ds_id, samp in session.exec(
                    select(DatasetSampleLink.dataset_id, Sample)
                    .join(Sample, Sample.id == DatasetSampleLink.sample_id)
                    .where(DatasetSampleLink.dataset_id.in_(dataset_ids))
                ).all():
                    samples_by_dataset.setdefault(ds_id, []).append(
                        LinkedResource(unique_id=samp.unique_id, resource_type="sample", name=samp.sample_name, relationship="associated")
                    )

        # Batch-resolve owners if requested
        owners_by_orcid = {}
        if include_owner:
            orcids = {row[0].owner_orcid for row in results if row[0].owner_orcid}
            if orcids:
                for u in session.exec(select(User).where(User.unique_id.in_(orcids))).all():
                    owners_by_orcid[u.unique_id] = UserPublicRead.model_validate(u)

        items = []
        for row in results:
            if include_metadata:
                dataset, creation_time, modification_time, public, sci_md, resolved_project_id, resolved_instrument_id = row
            else:
                dataset, creation_time, modification_time, public, resolved_project_id, resolved_instrument_id = row
                sci_md = None
            links = None
            if include_links:
                links = (
                    parents_by_dataset.get(dataset.id, []) +
                    children_by_dataset.get(dataset.id, []) +
                    samples_by_dataset.get(dataset.id, [])
                )
            items.append(DatasetResponse.model_validate({
                **dataset.model_dump(),
                "instrument_id": resolved_instrument_id,
                "project_id": resolved_project_id if resolved_project_id is not None else dataset.project_id,
                "public": public if public is not None else False,
                "creation_time": creation_time,
                "modification_time": modification_time,
                "scientific_metadata": sci_md,
                "links": links,
                "owner": owners_by_orcid.get(dataset.owner_orcid),
            }))
        next_cursor = results[-1][0].unique_id if len(results) == limit else None
        resp_offset = offset if (cursor is None and offset) else None
        return PaginatedResponse(total=total, limit=limit, offset=resp_offset, items=items, next_cursor=next_cursor)


def _dataset_base_query(principal):
    """Auth-scoped, deletion-filtered base query for datasets. Caller adds
    ordering and pagination. Joins Project/Instrument directly so callers get
    the live-resolved project_id/instrument_id alongside each row, rather
    than a second resolve query after the fact."""
    auth_filter = permission_filter(principal, Dataset)
    deletion_filter = deletion_filter_for(Dataset)
    return (
        select(Dataset, Resource.creation_time, Resource.modification_time, Resource.public,
               Project.project_id, Instrument.instrument_id)
        .join(Resource, Resource.mfid == Dataset.unique_id, isouter=True)
        .outerjoin(Project, Project.unique_id == Dataset.project_mfid)
        .outerjoin(Instrument, Instrument.unique_id == Dataset.instrument_mfid)
        .where(auth_filter)
        .where(deletion_filter)
    )


@router.get("/datasets/search", response_model=PaginatedResponse[DatasetResponse])
def search_datasets(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    q: str = Query(min_length=3, description="Search term matched against dataset_name."),
    project_id: Optional[str] = None,
    limit: int = Query(default=20, ge=1, le=50),
):
    '''
    Search datasets by name. Returns results ordered by match quality (exact → prefix → substring).
    Optionally scope to a project with ?project_id=. Maximum 50 results.
    '''

    with Session(engine) as session:
        session.execute(text(f"SET LOCAL pg_trgm.word_similarity_threshold = {get_trgm_search_threshold()}"))
        query = _dataset_base_query(auth_res)
        for token in q.lower().split():
            query = query.where(
                Dataset.dataset_name.ilike(f"{token}%") |
                literal(token).op("<%")(Dataset.dataset_name)
            )
        if project_id:
            query = query.where(Dataset.project_mfid == resolve_project_id_to_mfid(project_id, session))
        query = query.order_by(func.word_similarity(q, Dataset.dataset_name).desc()).limit(limit)
        results = session.exec(query).all()
        items = [DatasetResponse.model_validate({
            **dataset.model_dump(),
            "instrument_id": resolved_instrument_id,
            "project_id": resolved_project_id if resolved_project_id is not None else dataset.project_id,
            "public": public if public is not None else False,
            "creation_time": creation_time,
            "modification_time": modification_time,
        }) for dataset, creation_time, modification_time, public, resolved_project_id, resolved_instrument_id in results]
        return PaginatedResponse(limit=limit, items=items)


@router.get("/datasets/{dsid}", response_model=DatasetResponse)
def read_dataset(auth_res: Annotated[Principal, Depends(authorize_user)], dsid: str,
                 include_metadata: bool = False, include_links: bool = False,
                 include_owner: bool = False):
    '''
    Returns a dataset found with the provided unique ID.
    Pass include_links=true to include parent, child, and associated resources.
    Pass include_owner=true to resolve owner_orcid to a user object.
    '''

    with Session(engine) as session:
        
        # get with metadata or not
        resource_opts = (
            selectinload(Dataset.resource).selectinload(Resource.scientific_metadata)
            if include_metadata else selectinload(Dataset.resource)
        )
        link_opts = (
            [selectinload(Dataset.parents), selectinload(Dataset.children), selectinload(Dataset.samples)]
            if include_links else []
        )

        # fetch resource
        ds = session.exec(
            select(Dataset)
            .options(resource_opts, *link_opts)
            .where(Dataset.unique_id == dsid)
        ).one_or_none()

        # if not existing, return error
        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found")

        # check that requester has read access
        require_permission(session, auth_res, dsid, Permission.READ)

        # check deletion request
        deletion_req = get_active_deletion_request(session, dsid)

        # fetch all linked resources
        links = None
        if include_links:
            links = (
                [LinkedResource(unique_id=d.unique_id, resource_type="dataset",
                                name=d.dataset_name, relationship="parent") for d in ds.parents] +
                [LinkedResource(unique_id=d.unique_id, resource_type="dataset",
                                name=d.dataset_name, relationship="child") for d in ds.children] +
                [LinkedResource(unique_id=s.unique_id, resource_type="sample",
                                name=s.sample_name, relationship="associated") for s in ds.samples]
            )

        resource_md = ds.resource.scientific_metadata if ds.resource else None
        owner = None
        if include_owner and ds.owner_orcid:
            owner_user = session.exec(select(User).where(User.unique_id == ds.owner_orcid)).one_or_none()
            if owner_user:
                owner = UserPublicRead.model_validate(owner_user)
        instrument_ids = resolve_instrument_mfids_to_ids([ds.instrument_mfid], session)
        project_ids = resolve_project_mfids_to_ids([ds.project_mfid], session)
        return DatasetResponse.model_validate({
            **ds.model_dump(),
            "instrument_id": instrument_ids.get(ds.instrument_mfid),
            "project_id": project_ids.get(ds.project_mfid, ds.project_id),
            "public": ds.resource.public if ds.resource else ds.public,
            "creation_time": ds.resource.creation_time if ds.resource else None,
            "modification_time": ds.resource.modification_time if ds.resource else None,
            "scientific_metadata": resource_md.scientific_metadata if include_metadata and resource_md else None,
            "deletion_request": deletion_req.model_dump() if deletion_req else None,
            "links": links,
            "owner": owner,
        })


@router.post("/datasets/{parent_dataset_id}/children/{child_dataset_id}", response_model=DatasetLink)
def link_dataset_parent_child(parent_dataset_id, child_dataset_id, auth_res: Annotated[Principal, Depends(authorize_user)]):

    with Session(engine) as session:
        parent = session.exec(select(Dataset).where(Dataset.unique_id == parent_dataset_id)).first()
        if not parent:
            raise HTTPException(status_code=404, detail="Parent dataset not found")
        child = session.exec(select(Dataset).where(Dataset.unique_id == child_dataset_id)).first()
        if not child:
            raise HTTPException(status_code=404, detail="Child dataset not found")

        # Both, not either: the pre-role check passed if any one was


        # writable, so a link could be made to a resource you cannot edit.


        require_permission(session, auth_res, parent_dataset_id, Permission.EDIT)


        require_permission(session, auth_res, child_dataset_id, Permission.EDIT)

        link = add_or_update(session,
                     DatasetLink,
                     where=and_(DatasetLink.parent_dataset_id == parent.id, DatasetLink.child_dataset_id == child.id),
                     parent_dataset_id=parent.id,
                     child_dataset_id=child.id)

        touch_resource_modification_time(session, parent_dataset_id)
        touch_resource_modification_time(session, child_dataset_id)

        session.commit()
        session.refresh(link)
        return link


@router.delete("/datasets/{parent_dataset_id}/children/{child_dataset_id}")
def unlink_dataset_parent_child(parent_dataset_id: str, child_dataset_id: str, auth_res: Annotated[Principal, Depends(authorize_user)]):

    with Session(engine) as session:
        parent = session.exec(select(Dataset).where(Dataset.unique_id == parent_dataset_id)).one_or_none()
        if not parent:
            raise HTTPException(status_code=404, detail="Parent dataset not found")
        child = session.exec(select(Dataset).where(Dataset.unique_id == child_dataset_id)).one_or_none()
        if not child:
            raise HTTPException(status_code=404, detail="Child dataset not found")

        # Both, not either: the pre-role check passed if any one was


        # writable, so a link could be made to a resource you cannot edit.


        require_permission(session, auth_res, parent_dataset_id, Permission.EDIT)


        require_permission(session, auth_res, child_dataset_id, Permission.EDIT)

        link = session.exec(
            select(DatasetLink).where(
                and_(DatasetLink.parent_dataset_id == parent.id, DatasetLink.child_dataset_id == child.id)
            )
        ).one_or_none()
        if link is None:
            raise HTTPException(status_code=404, detail="No link found between these datasets")

        session.delete(link)
        touch_resource_modification_time(session, parent_dataset_id)
        touch_resource_modification_time(session, child_dataset_id)
        session.commit()
        return {"message": f"Successfully unlinked {parent_dataset_id} -> {child_dataset_id}"}


@router.get("/datasets/{child_dataset_id}/parents", response_model=PaginatedResponse[DatasetRead])
def list_parent_datasets(child_dataset_id: str, auth_res: Annotated[Principal, Depends(authorize_user)],
                         limit: int = Query(default=100, ge=1, le=1000),
                         offset: int = Query(default=0, ge=0)):
    '''
    Returns a paginated list of parent datasets for a given dataset.
    '''
    with Session(engine) as session:
        child = session.exec(select(Dataset).where(Dataset.unique_id == child_dataset_id)).one_or_none()
        if child is None:
            raise HTTPException(status_code=404, detail="Child dataset not found")
        require_permission(session, auth_res, child_dataset_id, Permission.READ)

        auth_filter = permission_filter(auth_res, Dataset)

        deletion_filter = deletion_filter_for(Dataset)
        query = (
            select(Dataset)
            .join(DatasetLink, DatasetLink.parent_dataset_id == Dataset.id)
            .join(Resource, Resource.mfid == Dataset.unique_id, isouter=True)
            .where(DatasetLink.child_dataset_id == child.id)
            .where(auth_filter).where(deletion_filter)
        )
        total = session.exec(select(func.count()).select_from(query.subquery())).one()
        parents = session.exec(query.offset(offset).limit(limit)).all()
        instrument_ids = resolve_instrument_mfids_to_ids((d.instrument_mfid for d in parents), session)
        project_ids = resolve_project_mfids_to_ids((d.project_mfid for d in parents), session)
        items = [DatasetRead.model_validate({
            **d.model_dump(),
            "instrument_id": instrument_ids.get(d.instrument_mfid),
            "project_id": project_ids.get(d.project_mfid, d.project_id),
        }) for d in parents]
        return PaginatedResponse(total=total, limit=limit, offset=offset, items=items)


@router.get("/datasets/{parent_dataset_id}/children", response_model=PaginatedResponse[DatasetRead])
def list_child_datasets(parent_dataset_id: str, auth_res: Annotated[Principal, Depends(authorize_user)],
                        limit: int = Query(default=100, ge=1, le=1000),
                        offset: int = Query(default=0, ge=0)):
    '''
    Returns a paginated list of child datasets for a given parent dataset.
    '''
    with Session(engine) as session:
        parent = session.exec(select(Dataset).where(Dataset.unique_id == parent_dataset_id)).one_or_none()
        if parent is None:
            raise HTTPException(status_code=404, detail="Parent dataset not found")
        require_permission(session, auth_res, parent_dataset_id, Permission.READ)

        auth_filter = permission_filter(auth_res, Dataset)

        deletion_filter = deletion_filter_for(Dataset)
        query = (
            select(Dataset)
            .join(DatasetLink, DatasetLink.child_dataset_id == Dataset.id)
            .join(Resource, Resource.mfid == Dataset.unique_id, isouter=True)
            .where(DatasetLink.parent_dataset_id == parent.id)
            .where(auth_filter).where(deletion_filter)
        )
        total = session.exec(select(func.count()).select_from(query.subquery())).one()
        children = session.exec(query.offset(offset).limit(limit)).all()
        instrument_ids = resolve_instrument_mfids_to_ids((d.instrument_mfid for d in children), session)
        project_ids = resolve_project_mfids_to_ids((d.project_mfid for d in children), session)
        items = [DatasetRead.model_validate({
            **d.model_dump(),
            "instrument_id": instrument_ids.get(d.instrument_mfid),
            "project_id": project_ids.get(d.project_mfid, d.project_id),
        }) for d in children]
        return PaginatedResponse(total=total, limit=limit, offset=offset, items=items)




@router.delete('/datasets/{dsid}/samples/{sample_id}')
def unlink_dataset_sample(auth_res: Annotated[Principal, Depends(authorize_user)], dsid: str, sample_id: str):

    with Session(engine) as session:
        ds = session.exec(select(Dataset).where(Dataset.unique_id == dsid)).one_or_none()
        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found")
        samp = session.exec(select(Sample).where(Sample.unique_id == sample_id)).one_or_none()
        if samp is None:
            raise HTTPException(status_code=404, detail="Sample not found")

        # Both, not either: the pre-role check passed if any one was


        # writable, so a link could be made to a resource you cannot edit.


        require_permission(session, auth_res, dsid, Permission.EDIT)


        require_permission(session, auth_res, sample_id, Permission.EDIT)

        link = session.exec(
            select(DatasetSampleLink).where(
                and_(DatasetSampleLink.dataset_id == ds.id, DatasetSampleLink.sample_id == samp.id)
            )
        ).one_or_none()
        if link is None:
            raise HTTPException(status_code=404, detail=f"No link found between dataset {dsid} and sample {sample_id}")

        session.delete(link)
        touch_resource_modification_time(session, dsid)
        touch_resource_modification_time(session, sample_id)
        session.commit()
        return {"message": f"Successfully unlinked dataset {dsid} and sample {sample_id}"}



@router.get("/datasets/{dsid}/thumbnails", response_model=List[ThumbnailRead])
def read_thumbnails(auth_res: Annotated[Principal, Depends(authorize_user)], dsid: str):
    '''
    Returns a list of thumbnail objects associated with the dataset ID provided
    '''
    with Session(engine) as session:
        ds_cmd = (select(Dataset)
                  .where(Dataset.unique_id == dsid)
                  .where(permission_filter(auth_res, Dataset, Permission.READ)))
        ds = session.exec(ds_cmd).one_or_none()

        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found or access denied")

        ds_thumbnails = session.exec(select(Thumbnail).where(Thumbnail.dataset_id == ds.id)).all()
        return ds_thumbnails


@router.post("/datasets/first_thumbnails")
def get_first_thumbnails(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    dataset_ids: List[str] = Body(...),
):
    '''
    Given a list of dataset unique_ids, returns a dict mapping each unique_id
    to its first (lowest-id) thumbnail. Datasets with no thumbnail or that the
    caller cannot access are omitted from the response.

    Intended for bulk thumbnail fetching after an entity_graph call.
    '''

    with Session(engine) as session:
        auth_filter = permission_filter(auth_res, Dataset, Permission.READ)

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
def add_thumbnail(auth_res: Annotated[Principal, Depends(authorize_user)],
                  dsid: str,
                  thumbnail: ThumbnailCreate):
    '''
    Adds a thumbnail to a dataset. If the thumbnail exists, it will simply add the existing thumbnail to the dataset object.
    Otherwise, the thumbnail will be created and then added to the dataset.

    Raises:
        HTTPException: If required thumbnail fields are missing or dataset not found
    '''

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

    # Resize to max 512px on longest side, re-encode as JPEG preserving aspect ratio
    try:
        import base64, io
        from PIL import Image
        raw = base64.b64decode(thumbnail.thumbnail_b64str)
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        img.thumbnail((512, 512), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        thumbnail_b64str = base64.b64encode(buf.getvalue()).decode("utf-8")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid image data: {e}")

    ds_cmd = (select(Dataset)
              .where(Dataset.unique_id == dsid)
              .where(permission_filter(auth_res, Dataset, Permission.EDIT)))

    with Session(engine) as session:
        ds = session.exec(ds_cmd).one_or_none()

        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found")

        tn_obj = add_or_update(session, Thumbnail, where=(Thumbnail.thumbnail_b64str == thumbnail_b64str),
                                thumbnail_name = thumbnail.thumbnail_name,
                                thumbnail_b64str = thumbnail_b64str,
                                dataset_id = ds.id)
        if not (tn_obj in ds.thumbnails):
            ds.thumbnails.append(tn_obj)
            session.add(ds)

        ri = session.exec(select(Resource).where(Resource.mfid == dsid)).one_or_none()
        if ri:
            ri.modification_time = get_utc_isoformat()

        session.commit()
        session.refresh(tn_obj)

    return(tn_obj)


@router.delete("/datasets/{dsid}/thumbnails/{thumbnail_id}")
def delete_thumbnail(auth_res: Annotated[Principal, Depends(authorize_user)], dsid: str, thumbnail_id: int):
    '''Removes a thumbnail from a dataset and deletes it.'''

    with Session(engine) as session:
        ds = session.exec(select(Dataset).where(Dataset.unique_id == dsid)).one_or_none()
        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found")

        require_permission(session, auth_res, dsid, Permission.EDIT)

        tn = session.exec(
            select(Thumbnail).where(Thumbnail.id == thumbnail_id, Thumbnail.dataset_id == ds.id)
        ).one_or_none()
        if tn is None:
            raise HTTPException(status_code=404, detail="Thumbnail not found")

        session.delete(tn)
        touch_resource_modification_time(session, dsid)
        session.commit()
        return {"message": f"Thumbnail {thumbnail_id} deleted from dataset {dsid}"}


@router.get("/datasets/{dsid}/download_links")
def generate_dataset_download_links(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    dsid: str,
):
    '''
    Returns signed GCS download URLs (1-hour expiry) for all files in the dataset,
    keyed by AssociatedFile mfid. Backfills missing storage_path records on the fly.
    '''

    with Session(engine) as session:
        ds = session.exec(select(Dataset).where(Dataset.unique_id == dsid)).one_or_none()
        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found")
        require_permission(session, auth_res, dsid, Permission.READ)

        af_records = session.exec(
            select(AssociatedFile).where(AssociatedFile.dataset_id == ds.id)
        ).all()

        mfid_to_path = {}
        for af in af_records:
            if af.storage_backend != "gcs":
                mfid_to_path[af.mfid] = None
                continue
            try:
                mfid_to_path[af.mfid] = _ensure_storage_path(af, dsid, session)
            except HTTPException:
                mfid_to_path[af.mfid] = None
        session.commit()

    # Generate signed URLs outside the DB session; null for missing blobs
    result = {}
    for mfid, storage_path in mfid_to_path.items():
        if storage_path is None:
            result[mfid] = None
            continue
        try:
            url, _ = _generate_signed_url_link(storage_path)
            result[mfid] = url
        except HTTPException:
            result[mfid] = None
    return result


@router.get("/datasets/{dsid}/files", response_model=List[AssociatedFileRead])
def read_associated_files(auth_res: Annotated[Principal, Depends(authorize_user)], dsid: str):
    '''
    Returns a list of the files associated with the dataset ID provided.
    '''

    with Session(engine) as session:
        ds = session.exec(select(Dataset).where(Dataset.unique_id == dsid)).one_or_none()
        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found")
        require_permission(session, auth_res, dsid, Permission.READ)

        files = session.exec(
            select(AssociatedFile)
            .where(AssociatedFile.dataset_id == ds.id)
            .options(selectinload(AssociatedFile.dataset))
        ).all()
        return [AssociatedFileRead.model_validate(f, update={"dataset_mfid": f.dataset.unique_id}) for f in files]


@router.post("/datasets/{dsid}/files", response_model=AssociatedFileRead)
def add_associated_file(auth_res: Annotated[Principal, Depends(authorize_user)],
                        dsid: str,
                        associated_file: AssociatedFileCreate):
    '''
    Adds a file record to a dataset.
    Creates the record if one with the same sha256_hash and dataset does not already exist.
    Use POST /datasets/{dsid}/files/{file_id}/ingest to trigger ingestion.
    '''

    with Session(engine) as session:
        ds = session.exec(select(Dataset).where(Dataset.unique_id == dsid)).one_or_none()
        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found")
        require_permission(session, auth_res, dsid, Permission.EDIT)

        af_obj = add_or_update(session,
                               AssociatedFile,
                               where=((AssociatedFile.sha256_hash == associated_file.sha256_hash) &
                                      (AssociatedFile.dataset_id == ds.id)),
                               **associated_file.model_dump(),
                               dataset_id=ds.id)

        if af_obj not in ds.associated_files:
            ds.associated_files.append(af_obj)
            session.add(ds)

        ri = session.exec(select(Resource).where(Resource.mfid == dsid)).one_or_none()
        if ri:
            ri.modification_time = get_utc_isoformat()

        session.commit()
        session.refresh(af_obj)
        return AssociatedFileRead.model_validate(af_obj, update={"dataset_mfid": af_obj.dataset.unique_id})


@router.post("/datasets/{dsid}/files/{file_id}/ingest", response_model=IngestionRequest, deprecated = True)
def create_ds_file_ingestion_request(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    dsid: str,
    file_id: str,
    ingestion_class: Optional[str] = None,
):
    '''
    Creates an ingestion request for a file.
    Returns error if the dataset associated with the file is not found, or the user does not have access.
    Requires write access to the dataset.
    '''
    from app.api.routes.files import create_file_ingestion_request
    return create_file_ingestion_request(auth_res, file_id, ingestion_class)


@router.get("/datasets/{dsid}/keywords", response_model=List[Keyword])
def list_keywords_for_dataset(auth_res: Annotated[Principal, Depends(authorize_user)], dsid: str):


    with Session(engine) as session:
        ds_cmd = (select(Dataset)
                  .where(Dataset.unique_id == dsid)
                  .where(permission_filter(auth_res, Dataset, Permission.READ)))
        ds = session.exec(ds_cmd).one_or_none()

        if ds is None:
            raise HTTPException(status_code=403, detail="Dataset not found or access denied")

        ds_keywords = session.exec(select(Keyword).select_from(join(Keyword, DatasetKeywordLink, DatasetKeywordLink.keyword_id == Keyword.id)).where(DatasetKeywordLink.dataset_id == ds.id)).all()
        return ds_keywords


@router.post("/datasets/{dsid}/keywords", response_model=Keyword)
def add_keyword(auth_res: Annotated[Principal, Depends(authorize_user)], dsid: str, keyword: str):
    '''
    Adds a keyword to a dataset. If the keyword exists, it will simply add the existing keyword to the dataset object.
    Otherwise, the keyword will be created and then added to the dataset.
    '''
    ds_cmd = (select(Dataset)
              .where(Dataset.unique_id == dsid)
              .where(permission_filter(auth_res, Dataset, Permission.EDIT)))

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
        touch_resource_modification_time(session, dsid)
        session.commit()
        session.refresh(kw_obj)
        return(kw_obj)


@router.post("/datasets/{dsid}/samples/{sample_id}", response_model=DatasetSampleLinkResponse)
def link_dataset_sample(auth_res: Annotated[Principal, Depends(authorize_user)], dsid: str, sample_id: str):

    with Session(engine) as session:
        # Both, not either: the pre-role check passed if any one was

        # writable, so a link could be made to a resource you cannot edit.

        require_permission(session, auth_res, dsid, Permission.EDIT)

        require_permission(session, auth_res, sample_id, Permission.EDIT)

        ds = session.exec(select(Dataset).where(Dataset.unique_id == dsid)).one_or_none()
        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found")
        samp = session.exec(select(Sample).where(Sample.unique_id == sample_id)).one_or_none()
        if samp is None:
            raise HTTPException(status_code=404, detail="Sample not found")

        add_or_update(session,
                      DatasetSampleLink,
                      where=and_(DatasetSampleLink.sample_id == samp.id, DatasetSampleLink.dataset_id == ds.id),
                      sample_id=samp.id,
                      dataset_id=ds.id)

        touch_resource_modification_time(session, dsid)
        touch_resource_modification_time(session, sample_id)
        session.commit()
        return DatasetSampleLinkResponse(dataset_id=ds.unique_id, dataset_name=ds.dataset_name, sample_id=samp.unique_id, sample_type=samp.sample_type, sample_name=samp.sample_name)


@router.get("/datasets/{dsid}/samples", response_model=PaginatedResponse[SampleRead])
def read_samples_in_dataset(dsid: str, auth_res: Annotated[Principal, Depends(authorize_user)],
                            limit: int = Query(default=100, ge=1, le=1000),
                            offset: int = Query(default=0, ge=0)):
    '''
    Returns a paginated list of samples associated with the dataset ID provided.
    '''
    with Session(engine) as session:
        ds = session.exec(
            select(Dataset)
            .where(Dataset.unique_id == dsid)
            .where(permission_filter(auth_res, Dataset, Permission.READ))
        ).one_or_none()
        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found or access denied")

        query = (
            select(Sample)
            .select_from(join(Sample, DatasetSampleLink, DatasetSampleLink.sample_id == Sample.id))
            .where(DatasetSampleLink.dataset_id == ds.id)
        )
        total = session.exec(select(func.count()).select_from(query.subquery())).one()
        samples = session.exec(query.offset(offset).limit(limit)).all()
        project_ids = resolve_project_mfids_to_ids((s.project_mfid for s in samples), session)
        items = [SampleRead.model_validate({
            **s.model_dump(),
            "project_id": project_ids.get(s.project_mfid, s.project_id),
        }) for s in samples]
        return PaginatedResponse(total=total, limit=limit, offset=offset, items=items)

@router.get("/datasets/{dataset_id}/dataset_graph")
def dataset_graph(dataset_id: str, auth_res: Annotated[Principal, Depends(authorize_user)]):
    '''
    Returns a NetworkX node-link graph of all datasets connected (via parent/child
    relationships) to the given dataset. Loads all authorized datasets in the
    project and returns the directed lineage subgraph (ancestors + descendants) of the target.
    '''

    with Session(engine) as session:
        auth_filter = permission_filter(auth_res, Dataset)
        deletion_filter = deletion_filter_for(Dataset)

        target = session.exec(
            select(Dataset)
            .join(Resource, Resource.mfid == Dataset.unique_id, isouter=True)
            .where(auth_filter).where(deletion_filter)
            .where(Dataset.unique_id == dataset_id)
        ).first()

        if target is None:
            raise HTTPException(status_code=404, detail="Dataset not found")

        ds_list = session.exec(
            select(Dataset)
            .options(joinedload(Dataset.parents))
            .join(Resource, Resource.mfid == Dataset.unique_id, isouter=True)
            .where(auth_filter).where(deletion_filter)
            .where(Dataset.project_mfid == target.project_mfid)
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
def dataset_graph_cte(dataset_id: str, auth_res: Annotated[Principal, Depends(authorize_user)]):
    '''
    CTE-based implementation (kept for reference). Prefer /dataset_graph.
    Returns a NetworkX node-link graph of all datasets connected (via parent/child
    relationships) to the given dataset, i.e. its full lineage subgraph.
    '''

    with Session(engine) as session:
        auth_filter = permission_filter(auth_res, Dataset)
        deletion_filter = deletion_filter_for(Dataset)

        target = session.exec(
            select(Dataset)
            .join(Resource, Resource.mfid == Dataset.unique_id, isouter=True)
            .where(auth_filter).where(deletion_filter)
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

        # Restrict CTE results to datasets the user is authorized to read and
        # that don't have a pending/approved deletion request.
        all_uids = [r.unique_id for r in rows]
        visible_uids = set(session.exec(
            select(Dataset.unique_id)
            .join(Resource, Resource.mfid == Dataset.unique_id, isouter=True)
            .where(auth_filter).where(deletion_filter)
            .where(Dataset.unique_id.in_(all_uids))
        ).all()) if all_uids else set()

        rows = [r for r in rows if r.unique_id in visible_uids]
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


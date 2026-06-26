from typing import Annotated, List, Optional, Union
import logging

logger = logging.getLogger(__name__)

import os
import networkx as nx
from networkx.readwrite import json_graph
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from sqlalchemy import exists, func, join, text
from sqlalchemy.orm import joinedload, selectinload
from sqlmodel import and_, Session, select

from app.api.dependencies import get_engine
from app.general_utils import get_utc_isoformat, is_orcid
from app.models.core_models import (
    AccessGroup, Dataset, ResourceACL, DatasetSampleLink,
    DeletionRequest, Resource, Sample, SampleLink, ScientificMetadataBinary, User,
)
from app.models.datasets import DatasetRead
from app.models.links import LinkedResource
from app.models.pagination import PaginatedResponse
from app.models.samples import SampleCreate, SampleRead, SampleResponse, SampleUpdate
from app.utils.auth_utils import authorize_user, require_groups, require_write_access, require_read_access, read_access_filter_list
from app.utils.sql_utils import add_or_update, generate_kw_query, search_relevance_order, sql_create_resource_acl, create_standard_acls, get_active_deletion_request
from app.utils.gcs_record_utils import write_record, sample_record

router = APIRouter(tags=["samples"])
engine = get_engine()

API_VERSION = os.getenv('API_VERSION', None)
API_VERSION_GITHASH = os.getenv("GITHASH", None)

@router.post("/samples/{parent_sample_id}/children/{child_sample_id}", response_model=SampleLink)
def link_sample_parent_child(parent_sample_id, child_sample_id, auth_res: Annotated[tuple, Depends(authorize_user)]):
    _, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        require_write_access(session, ag_ids, [parent_sample_id, child_sample_id])

        parent_sample = session.exec(select(Sample).where(Sample.unique_id == parent_sample_id)).first()
        if not parent_sample:
            raise HTTPException(status_code=404, detail="Parent sample not found")
        child_sample = session.exec(select(Sample).where(Sample.unique_id == child_sample_id)).first()
        if not child_sample:
            raise HTTPException(status_code=404, detail="Child sample not found")

        link = add_or_update(session,
                     SampleLink,
                     where=and_(SampleLink.parent_sample_id == parent_sample.id, SampleLink.sample_id == child_sample.id),
                     parent_sample_id=parent_sample.id,
                     sample_id=child_sample.id)

        session.commit()
        session.refresh(link)
        return link


@router.delete("/samples/{parent_sample_id}/children/{child_sample_id}")
def unlink_sample_parent_child(parent_sample_id: str, child_sample_id: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    _, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        require_write_access(session, ag_ids, [parent_sample_id, child_sample_id])

        parent = session.exec(select(Sample).where(Sample.unique_id == parent_sample_id)).one_or_none()
        if not parent:
            raise HTTPException(status_code=404, detail="Parent sample not found")
        child = session.exec(select(Sample).where(Sample.unique_id == child_sample_id)).one_or_none()
        if not child:
            raise HTTPException(status_code=404, detail="Child sample not found")

        link = session.exec(
            select(SampleLink).where(
                and_(SampleLink.parent_sample_id == parent.id, SampleLink.sample_id == child.id)
            )
        ).one_or_none()
        if link is None:
            raise HTTPException(status_code=404, detail="No link found between these samples")

        session.delete(link)
        session.commit()
        return {"message": f"Successfully unlinked {parent_sample_id} -> {child_sample_id}"}


@router.patch('/samples/{sample_id}', response_model=SampleResponse)
def update_sample(auth_res: Annotated[tuple, Depends(authorize_user)], sample_id: str, updated_sample: SampleUpdate,
                  background_tasks: BackgroundTasks):
    '''
    Partially update a sample record. Only provided fields are modified.
    '''
    user_unique_id, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        sample = session.exec(select(Sample).where(Sample.unique_id == sample_id)).one_or_none()
        if not sample:
            raise HTTPException(status_code=404, detail='Sample not found')

        require_write_access(session, ag_ids, [sample_id])

        update_dict = updated_sample.model_dump(exclude_unset=True)
        new_public = update_dict.pop('public', None)
        for key, value in update_dict.items():
            setattr(sample, key, value)

        ri = session.exec(select(Resource).where(Resource.mfid == sample_id)).one_or_none()
        if ri:
            ri.modification_time = get_utc_isoformat()
            if new_public is not None:
                ri.public = new_public

        if 'owner_orcid' in update_dict:
            owner_ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == sample.owner_orcid)).one_or_none()
            sql_create_resource_acl(owner_ag, sample_id, read=True, write=True, session=session)

        if 'project_id' in update_dict:
            proj_ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == sample.project_id)).one_or_none()
            sql_create_resource_acl(proj_ag, sample_id, read=True, write=True, session=session)

        session.add(sample)
        session.commit()
        session.refresh(sample)
        background_tasks.add_task(write_record, sample.unique_id, sample_record(sample, ri), user_unique_id, "update")
        return SampleResponse.model_validate({
            **sample.model_dump(),
            "public": ri.public if ri else False,
            "creation_time": ri.creation_time if ri else None,
            "modification_time": ri.modification_time if ri else None,
        })


@router.post("/samples", response_model=SampleResponse)
def create_sample(auth_res: Annotated[tuple, Depends(authorize_user)], S: SampleCreate,
                  background_tasks: BackgroundTasks):
    '''
    Create a new sample. If a sample with the same project_id and sample_name already
    exists, the existing record is returned without modification.
    '''
    user_unique_id, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    if not S.sample_name.strip():
        raise HTTPException(status_code=400, detail="sample_name must not be empty.")

    with Session(engine) as session:
        # Dedup: return existing sample if (project_id, sample_name) already registered
        existing = session.exec(
            select(Sample).where(
                and_(Sample.project_id == S.project_id, Sample.sample_name == S.sample_name)
            )
        ).one_or_none()
        if existing:
            ri = session.exec(select(Resource).where(Resource.mfid == existing.unique_id)).one_or_none()
            return SampleResponse.model_validate({
                **existing.model_dump(),
                "public": ri.public if ri else False,
                "creation_time": ri.creation_time if ri else None,
                "modification_time": ri.modification_time if ri else None,
            })

        sample = Sample(**S.model_dump(exclude_unset=True))
        session.add(sample)
        session.commit()
        session.refresh(sample)

        # Resolve owner from auth if not explicitly provided
        if sample.owner_orcid is None and is_orcid(user_unique_id):
            sample.owner_orcid = user_unique_id
        if sample.owner_user_id is None and sample.owner_orcid:
            sample.owner_user_id = session.exec(
                select(User.id).where(User.unique_id == sample.owner_orcid)
            ).one_or_none()

        now = get_utc_isoformat()
        existing_ri = session.exec(select(Resource).where(Resource.mfid == sample.unique_id)).one_or_none()
        ri = add_or_update(session,
                           Resource,
                           where=(Resource.mfid == sample.unique_id),
                           mfid=sample.unique_id,
                           resource_type="sample",
                           created_with_api_version = API_VERSION,
                           api_version_githash = API_VERSION_GITHASH)
        if existing_ri is None:
            ri.creation_time = now
        ri.modification_time = now
        ri.public = S.public
        session.commit()

        create_standard_acls(session, sample.unique_id,
                             owner_id=sample.owner_orcid,
                             requestor_id=user_unique_id,
                             project_id=sample.project_id)

        session.add(sample)
        session.commit()
        session.refresh(sample)
        background_tasks.add_task(write_record, sample.unique_id, sample_record(sample, ri), user_unique_id, "create")
        return SampleResponse.model_validate({
            **sample.model_dump(),
            "public": ri.public if ri else False,
            "creation_time": ri.creation_time if ri else None,
            "modification_time": ri.modification_time if ri else None,
        })


@router.get("/samples", response_model=PaginatedResponse[SampleResponse])
def list_samples(auth_res: Annotated[tuple, Depends(authorize_user)], request: Request,
                 include_metadata: bool = False,
                 include_links: bool = False,
                 include_datasets: bool = False,
                 limit: int = Query(default=100, ge=1, le=1000),
                 cursor: Optional[str] = None,
                 offset: int = Query(default=0, ge=0),
                 creation_time_gte: Optional[str] = None, creation_time_lte: Optional[str] = None,
                 modification_time_gte: Optional[str] = None, modification_time_lte: Optional[str] = None):
    '''
    Returns a paginated list of samples the requesting user is authorized to view.
    Default limit is 100, max is 1000.

    Keyset (cursor) pagination: results are ordered by unique_id descending, which is
    creation order since unique_id is a time-sortable UUIDv7. To fetch the next page,
    pass cursor=<next_cursor from the previous response>. The first page omits cursor.
    next_cursor is null when there are no further pages. total is included only on the
    first page (cursor omitted); subsequent pages omit it to avoid a redundant count.

    Offset pagination is supported for backward compatibility: pass offset=<n> instead
    of cursor. cursor takes precedence when both are given. Prefer cursor for large
    result sets, since deep offsets are slower.
    Pass include_links=true to include parent, child, and associated resources for each sample.
    include_datasets is deprecated — use include_links instead.
    '''
    import warnings
    if include_datasets:
        warnings.warn("include_datasets is deprecated, use include_links=true instead", DeprecationWarning)
        include_links = True

    _, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    params_dict = dict(request.query_params)
    for k in ('creation_time_gte', 'creation_time_lte', 'modification_time_gte',
              'modification_time_lte', 'include_metadata', 'include_links', 'include_datasets', 'limit', 'cursor', 'offset'):
        params_dict.pop(k, None)

    with Session(engine) as session:
        auth_filter, deletion_filter = read_access_filter_list(Sample, ag_ids)

        def apply_filters(q):
            q = q.where(auth_filter).where(deletion_filter)
            if creation_time_gte:
                q = q.where(Resource.creation_time >= creation_time_gte)
            if creation_time_lte:
                q = q.where(Resource.creation_time <= creation_time_lte)
            if modification_time_gte:
                q = q.where(Resource.modification_time >= modification_time_gte)
            if modification_time_lte:
                q = q.where(Resource.modification_time <= modification_time_lte)
            for k, v in params_dict.items():
                if hasattr(Sample, k) and v is not None:
                    q = q.where(getattr(Sample, k) == v)
            return q

        # total is only needed on the first page; cursor pages and deep offset pages
        # skip the redundant count.
        total = None
        if cursor is None and offset == 0:
            total = session.exec(apply_filters(
                select(func.count()).select_from(Sample)
                .join(Resource, Resource.mfid == Sample.unique_id, isouter=True)
            )).one()

        if include_metadata:
            samp_cmd = (
                select(Sample, Resource.creation_time, Resource.modification_time, Resource.public, ScientificMetadataBinary.scientific_metadata)
                .join(Resource, Resource.mfid == Sample.unique_id, isouter=True)
                .join(ScientificMetadataBinary, ScientificMetadataBinary.unique_id == Sample.unique_id, isouter=True)
            )
        else:
            samp_cmd = (
                select(Sample, Resource.creation_time, Resource.modification_time, Resource.public)
                .join(Resource, Resource.mfid == Sample.unique_id, isouter=True)
            )

        samp_cmd = apply_filters(samp_cmd)
        if cursor is not None:
            samp_cmd = samp_cmd.where(Sample.unique_id < cursor)
        samp_cmd = samp_cmd.order_by(Sample.unique_id.desc())
        if cursor is None and offset:
            samp_cmd = samp_cmd.offset(offset)
        samp_cmd = samp_cmd.limit(limit)
        rows = session.exec(samp_cmd).all()

        if include_links:
            sample_ids = [row[0].id for row in rows]
            parents_by_sample: dict = {}
            children_by_sample: dict = {}
            datasets_by_sample: dict = {}
            dataset_reads_by_sample: dict = {}
            if sample_ids:
                for child_id, parent in session.exec(
                    select(SampleLink.sample_id, Sample)
                    .join(Sample, Sample.id == SampleLink.parent_sample_id)
                    .where(SampleLink.sample_id.in_(sample_ids))
                ).all():
                    parents_by_sample.setdefault(child_id, []).append(
                        LinkedResource(unique_id=parent.unique_id, resource_type="sample", name=parent.sample_name, relationship="parent")
                    )
                for parent_id, child in session.exec(
                    select(SampleLink.parent_sample_id, Sample)
                    .join(Sample, Sample.id == SampleLink.sample_id)
                    .where(SampleLink.parent_sample_id.in_(sample_ids))
                ).all():
                    children_by_sample.setdefault(parent_id, []).append(
                        LinkedResource(unique_id=child.unique_id, resource_type="sample", name=child.sample_name, relationship="child")
                    )
                for sid, ds in session.exec(
                    select(DatasetSampleLink.sample_id, Dataset)
                    .join(Dataset, Dataset.id == DatasetSampleLink.dataset_id)
                    .where(DatasetSampleLink.sample_id.in_(sample_ids))
                ).all():
                    datasets_by_sample.setdefault(sid, []).append(
                        LinkedResource(unique_id=ds.unique_id, resource_type="dataset", name=ds.dataset_name, relationship="associated")
                    )
                    dataset_reads_by_sample.setdefault(sid, []).append(DatasetRead.model_validate(ds))

        items = []
        for row in rows:
            if include_metadata:
                samp, creation_time, modification_time, public, sci_md = row
            else:
                samp, creation_time, modification_time, public = row
                sci_md = None
            links = None
            compat_datasets = None
            if include_links:
                links = (
                    parents_by_sample.get(samp.id, []) +
                    children_by_sample.get(samp.id, []) +
                    datasets_by_sample.get(samp.id, [])
                )
                if include_datasets:
                    compat_datasets = dataset_reads_by_sample.get(samp.id, [])
            items.append(SampleResponse.model_validate({
                **samp.model_dump(),
                "public": public if public is not None else False,
                "creation_time": creation_time,
                "modification_time": modification_time,
                "scientific_metadata": sci_md,
                "datasets": compat_datasets,
                "links": links,
            }))
        next_cursor = rows[-1][0].unique_id if len(rows) == limit else None
        resp_offset = offset if (cursor is None and offset) else None
        return PaginatedResponse(total=total, limit=limit, offset=resp_offset, items=items, next_cursor=next_cursor)


def _sample_base_query(ag_ids: list):
    """Auth-scoped, deletion-filtered base query for samples. Caller adds ordering and pagination."""
    auth_filter, deletion_filter = read_access_filter_list(Sample, ag_ids)
    return (
        select(Sample, Resource.creation_time, Resource.modification_time, Resource.public)
        .join(Resource, Resource.mfid == Sample.unique_id, isouter=True)
        .where(auth_filter)
        .where(deletion_filter)
    )


@router.get("/samples/search", response_model=PaginatedResponse[SampleResponse])
def search_samples(
    auth_res: Annotated[tuple, Depends(authorize_user)],
    q: str = Query(min_length=3, description="Search term matched against sample_name."),
    project_id: Optional[str] = None,
    limit: int = Query(default=20, ge=1, le=50),
):
    '''
    Search samples by name. Returns results ordered by match quality (exact → prefix → substring).
    Optionally scope to a project with ?project_id=. Maximum 50 results.
    '''
    _, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        query = _sample_base_query(ag_ids)
        query = query.where(Sample.sample_name.ilike(f"%{q}%"))
        if project_id:
            query = query.where(Sample.project_id == project_id)
        query = query.order_by(search_relevance_order(Sample.sample_name, q), Sample.sample_name).limit(limit)
        results = session.exec(query).all()
        items = [SampleResponse.model_validate({
            **samp.model_dump(),
            "public": public if public is not None else False,
            "creation_time": creation_time,
            "modification_time": modification_time,
        }) for samp, creation_time, modification_time, public in results]
        return PaginatedResponse(limit=limit, items=items)


@router.get("/samples/{sample_id}", response_model=SampleResponse)
def read_sample(sample_id: str, auth_res: Annotated[tuple, Depends(authorize_user)], include_links: bool = False, include_metadata: bool = False):
    '''
    Returns the sample for the provided ID.
    Pass include_links=true to include parent, child, and associated resources.
    '''
    _, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]
    with Session(engine) as session:
        # get with metadata or not
        resource_opts = (
            selectinload(Sample.resource).selectinload(Resource.scientific_metadata)
            if include_metadata else selectinload(Sample.resource)
        )
        link_opts = (
            [selectinload(Sample.parents), selectinload(Sample.children)]
            if include_links else []
        )

        # fetch resource
        samp = session.exec(
            select(Sample)
            .options(selectinload(Sample.datasets), resource_opts, *link_opts)
            .where(Sample.unique_id == sample_id)
        ).first()

        # if not existing, return error
        if samp is None:
            raise HTTPException(status_code=404, detail="Sample not found")

        # check that requester has read access
        require_read_access(session, sample_id, ag_ids)

        # check deletion request
        deletion_req = get_active_deletion_request(session, sample_id)

        # fetch all linked resources (datasets reuse already-loaded relationship)
        links = None
        if include_links:
            links = (
                [LinkedResource(unique_id=s.unique_id, resource_type="sample", name=s.sample_name, relationship="parent") for s in samp.parents] +
                [LinkedResource(unique_id=s.unique_id, resource_type="sample", name=s.sample_name, relationship="child") for s in samp.children] +
                [LinkedResource(unique_id=d.unique_id, resource_type="dataset", name=d.dataset_name, relationship="associated") for d in samp.datasets]
            )

        resource_md = samp.resource.scientific_metadata if samp.resource else None
        return SampleResponse.model_validate({
            **samp.model_dump(),
            "public": samp.resource.public if samp.resource else False,
            "datasets": [DatasetRead.model_validate(ds) for ds in samp.datasets],
            "creation_time": samp.resource.creation_time if samp.resource else None,
            "modification_time": samp.resource.modification_time if samp.resource else None,
            "scientific_metadata": resource_md.scientific_metadata if include_metadata and resource_md else None,
            "deletion_request": deletion_req.model_dump() if deletion_req else None,
            "links": links,
        })


@router.get("/samples/{sample_id}/datasets")
def list_sample_datasets(sample_id: str, auth_res: Annotated[tuple, Depends(authorize_user)], request: Request, include_metadata: bool = False, limit: Optional[int] = None):
    '''
    Returns a list of all datasets linked to a given sample
    '''
    user_unique_id, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    params_dict = dict(request.query_params)
    keyword_filter = params_dict.pop("keyword", None)

    with Session(engine) as session:
        # Verify sample exists
        sample = session.exec(select(Sample).where(Sample.unique_id == sample_id)).one_or_none()

        if sample is None:
            raise HTTPException(status_code=404, detail="Sample not found")

        auth_filter = (ResourceACL.accessgroup_id.in_(ag_ids)) | (Dataset.public == True)
        if include_metadata:
            query = (
                    select(Dataset, ScientificMetadataBinary.scientific_metadata)
                    .join(ResourceACL, ResourceACL.mfid == Dataset.unique_id, isouter=True)
                    .join(ScientificMetadataBinary, ScientificMetadataBinary.unique_id == Dataset.unique_id, isouter=True)
                    .filter(auth_filter)
                )

        else:
            query = (select(Dataset)
                        .select_from(
                            join(Dataset, ResourceACL, ResourceACL.mfid == Dataset.unique_id, isouter=True)
                        )
                        .filter(auth_filter)
                    )

        if keyword_filter:
            query = generate_kw_query(session, query, keyword_filter)

        for key, value in params_dict.items():
            if hasattr(Dataset, key):
                query = query.where(getattr(Dataset, key) == value)

        full_query = query.join(DatasetSampleLink,
                                DatasetSampleLink.dataset_id == Dataset.id
                                ).where(
                                    DatasetSampleLink.sample_id == select(Sample.id).where(Sample.unique_id == sample_id)
                                ).where(
                                    ~exists().where(
                                        DeletionRequest.resource_id == Dataset.unique_id,
                                        DeletionRequest.status.in_(["pending", "approved"])
                                    )
                                ).distinct(Dataset.id)

        if limit is not None:
            full_query = full_query.limit(limit)

        results = session.exec(full_query).all()
        if include_metadata:
            return [{**dataset.model_dump(),
                    "scientific_metadata": sci_md
                    }
                    for dataset, sci_md in results]
        else:
            return results


@router.get("/samples/{child_sample_id}/parents", response_model=PaginatedResponse[SampleRead])
def list_parent_samples(child_sample_id: str, auth_res: Annotated[tuple, Depends(authorize_user)],
                        limit: int = Query(default=100, ge=1, le=1000),
                        offset: int = Query(default=0, ge=0)):
    '''
    Returns a paginated list of parent samples for a given child sample.
    '''
    with Session(engine) as session:
        child_sample = session.exec(select(Sample).where(Sample.unique_id == child_sample_id)).one_or_none()
        if child_sample is None:
            raise HTTPException(status_code=404, detail="Child sample not found")

        query = (
            select(Sample)
            .select_from(join(Sample, SampleLink, SampleLink.parent_sample_id == Sample.id))
            .where(SampleLink.sample_id == child_sample.id)
            .where(~exists().where(DeletionRequest.resource_id == Sample.unique_id, DeletionRequest.status.in_(["pending", "approved"])))
        )
        total = session.exec(select(func.count()).select_from(query.subquery())).one()
        items = session.exec(query.offset(offset).limit(limit)).all()
        return PaginatedResponse(total=total, limit=limit, offset=offset, items=items)


@router.get("/samples/{parent_sample_id}/children", response_model=PaginatedResponse[SampleRead])
def list_child_samples(parent_sample_id: str, auth_res: Annotated[tuple, Depends(authorize_user)],
                       limit: int = Query(default=100, ge=1, le=1000),
                       offset: int = Query(default=0, ge=0)):
    '''
    Returns a paginated list of child samples for a given parent sample.
    '''
    with Session(engine) as session:
        parent_sample = session.exec(select(Sample).where(Sample.unique_id == parent_sample_id)).one_or_none()
        if parent_sample is None:
            raise HTTPException(status_code=404, detail="Parent sample not found")

        query = (
            select(Sample)
            .select_from(join(Sample, SampleLink, SampleLink.sample_id == Sample.id))
            .where(SampleLink.parent_sample_id == parent_sample.id)
            .where(~exists().where(DeletionRequest.resource_id == Sample.unique_id, DeletionRequest.status.in_(["pending", "approved"])))
        )
        total = session.exec(select(func.count()).select_from(query.subquery())).one()
        items = session.exec(query.offset(offset).limit(limit)).all()
        return PaginatedResponse(total=total, limit=limit, offset=offset, items=items)

@router.get("/samples/{sample_id}/sample_graph")
def sample_graph(sample_id: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    '''
    Returns a NetworkX node-link graph of all samples connected (via parent/child
    relationships) to the given sample. Loads all samples in the project and
    returns the directed lineage subgraph (ancestors + descendants) of the target.
    '''
    _, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        target = session.exec(
            select(Sample).where(Sample.unique_id == sample_id)
        ).one_or_none()

        if target is None:
            raise HTTPException(status_code=404, detail="Sample not found")

        acl_ok = session.exec(
            select(ResourceACL)
            .where(ResourceACL.mfid == sample_id)
            .where(ResourceACL.accessgroup_id.in_(ag_ids))
        ).first()
        if acl_ok is None:
            raise HTTPException(status_code=403, detail="Access denied")

        samp_list = session.exec(
            select(Sample)
            .options(joinedload(Sample.parents))
            .where(Sample.project_id == target.project_id)
        ).unique().all()

        edges = []
        for sample in samp_list:
            for parent in sample.parents:
                edges.append((parent.unique_id, sample.unique_id))

        G = nx.DiGraph(edges)
        for sample in samp_list:
            G.add_node(sample.unique_id, sample_name=sample.sample_name)

        if target.unique_id not in G:
            G.add_node(target.unique_id, sample_name=target.sample_name)

        # Use directed ancestors + descendants rather than the weakly connected
        # component. WCC would include siblings (nodes that share a parent with
        # the target but are not in its direct lineage). nx.ancestors/descendants
        # traverses strictly along the target's own lineage chain in both
        # directions, excluding siblings.
        component_nodes = nx.ancestors(G, target.unique_id) | nx.descendants(G, target.unique_id) | {target.unique_id}
        return json_graph.node_link_data(G.subgraph(component_nodes))


@router.get("/samples/{sample_id}/sample_graph_cte")
def sample_graph_cte(sample_id: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    '''
    CTE-based implementation (kept for reference). Prefer /sample_graph.
    Returns a NetworkX node-link graph of all samples connected (via parent/child
    relationships) to the given sample, i.e. its full lineage subgraph.
    '''
    _, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        target = session.exec(
            select(Sample).where(Sample.unique_id == sample_id)
        ).one_or_none()

        if target is None:
            raise HTTPException(status_code=404, detail="Sample not found")

        acl_ok = session.exec(
            select(ResourceACL)
            .where(ResourceACL.mfid == sample_id)
            .where(ResourceACL.accessgroup_id.in_(ag_ids))
        ).first()
        if acl_ok is None:
            raise HTTPException(status_code=403, detail="Access denied")

        # Two recursive CTEs — one walking up (ancestors), one walking down
        # (descendants). PostgreSQL only allows a single UNION between the
        # non-recursive and recursive terms, so bidirectional traversal in a
        # single CTE is not permitted.
        cte_sql = text("""
            WITH RECURSIVE
            ancestors(id, unique_id, sample_name) AS (
                SELECT id, unique_id, sample_name
                FROM sample WHERE unique_id = :sample_uid
              UNION
                SELECT s.id, s.unique_id, s.sample_name
                FROM sample s
                JOIN samplelink sl ON sl.parent_sample_id = s.id
                JOIN ancestors a ON sl.sample_id = a.id
            ),
            descendants(id, unique_id, sample_name) AS (
                SELECT id, unique_id, sample_name
                FROM sample WHERE unique_id = :sample_uid
              UNION
                SELECT s.id, s.unique_id, s.sample_name
                FROM sample s
                JOIN samplelink sl ON sl.sample_id = s.id
                JOIN descendants d ON sl.parent_sample_id = d.id
            )
            SELECT DISTINCT id, unique_id, sample_name
            FROM (SELECT * FROM ancestors UNION ALL SELECT * FROM descendants) combined
        """)
        rows = session.execute(cte_sql, {"sample_uid": sample_id}).all()

        id_to_uid = {r.id: r.unique_id for r in rows}
        node_ids = list(id_to_uid.keys())

        # Fetch all edges whose both endpoints are within the component
        edge_rows = session.exec(
            select(SampleLink.parent_sample_id, SampleLink.sample_id)
            .where(SampleLink.parent_sample_id.in_(node_ids))
            .where(SampleLink.sample_id.in_(node_ids))
        ).all()

        G = nx.DiGraph((id_to_uid[r[0]], id_to_uid[r[1]]) for r in edge_rows)
        for r in rows:
            G.add_node(r.unique_id, sample_name=r.sample_name)

        return json_graph.node_link_data(G)
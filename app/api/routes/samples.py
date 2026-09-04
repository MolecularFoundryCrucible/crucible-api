from typing import Annotated, List, Literal, Optional, Union
import logging

logger = logging.getLogger(__name__)

import os
import networkx as nx
from networkx.readwrite import json_graph
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response
from sqlalchemy import exists, func, join, literal, text
from sqlmodel import and_, Session, select

from app.api.dependencies import get_engine, get_trgm_search_threshold
from app.general_utils import get_utc_isoformat
from app.models.core_models import (
    Dataset, DatasetSampleLink,
    DeletionRequest, Project, Resource, Sample, SampleLink, ScientificMetadataBinary, User,
)
from app.models.datasets import DatasetRead
from app.models.links import LinkedResource
from app.models.pagination import PaginatedResponse
from app.models.projects import ProjectReferenceRead
from app.models.samples import SampleCreate, SampleRead, SampleResponse, SampleUpdate
from app.utils.auth_utils import (
    access_selector_filter, authorize_user, permission_filter, require_permission)
from app.utils.instrument_utils import resolve_instrument_mfids_to_references
from app.utils.permissions import Principal, Permission, Role
from app.utils.project_utils import project_standing, resolve_project_id_to_mfid, resolve_project_mfids_to_references
from app.utils.sql_utils import add_or_update, assert_mfid_available, deletion_filter_for, generate_kw_query, create_standard_acls, touch_resource_modification_time, set_public, visibility_filter_for
from app.utils.gcs_record_utils import write_record, sample_record
from app.utils.user_utils import resolve_create_owner
from app.models.users import UserPublicRead
from app.services.resource_details import ResourceDetailOptions, load_sample_detail

router = APIRouter(tags=["samples"])
engine = get_engine()
RELATIONSHIP_ROUTE_DEPRECATION = "@1788307200"

API_VERSION = os.getenv('API_VERSION', None)
API_VERSION_GITHASH = os.getenv("GITHASH", None)

@router.post("/samples/{parent_sample_id}/children/{child_sample_id}", response_model=SampleLink)
def link_sample_parent_child(parent_sample_id, child_sample_id, auth_res: Annotated[Principal, Depends(authorize_user)],
                             relationship_type: Optional[Literal["is_derived_from", "is_part_of"]] = Query(default=None)):
    '''
    Link two samples as parent and child.

    relationship_type describes the child relative to the parent, so
    is_part_of means "child is_part_of parent". Omit it to leave the kind
    unspecified; on a link that already exists, omitting it preserves the
    type already stored rather than clearing it.
    '''

    with Session(engine) as session:
        # Both, not either: the pre-role check passed on any one.

        require_permission(session, auth_res, parent_sample_id, Permission.EDIT)

        require_permission(session, auth_res, child_sample_id, Permission.EDIT)

        parent_sample = session.exec(select(Sample).where(Sample.unique_id == parent_sample_id)).first()
        if not parent_sample:
            raise HTTPException(status_code=404, detail="Parent sample not found")
        child_sample = session.exec(select(Sample).where(Sample.unique_id == child_sample_id)).first()
        if not child_sample:
            raise HTTPException(status_code=404, detail="Child sample not found")

        # Only pass relationship_type when the caller supplied one: add_or_update
        # overwrites every kwarg it is given, so passing None would blank the
        # stored type on a re-POST that just meant "make sure this link exists".
        link_fields = {}
        if relationship_type is not None:
            link_fields["relationship_type"] = relationship_type

        link = add_or_update(session,
                     SampleLink,
                     where=and_(SampleLink.parent_sample_id == parent_sample.id, SampleLink.sample_id == child_sample.id),
                     parent_sample_id=parent_sample.id,
                     sample_id=child_sample.id,
                     **link_fields)

        touch_resource_modification_time(session, parent_sample_id)
        touch_resource_modification_time(session, child_sample_id)

        session.commit()
        session.refresh(link)
        return link


@router.delete("/samples/{parent_sample_id}/children/{child_sample_id}")
def unlink_sample_parent_child(parent_sample_id: str, child_sample_id: str, auth_res: Annotated[Principal, Depends(authorize_user)]):

    with Session(engine) as session:
        # Both, not either: the pre-role check passed on any one.

        require_permission(session, auth_res, parent_sample_id, Permission.EDIT)

        require_permission(session, auth_res, child_sample_id, Permission.EDIT)

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
        touch_resource_modification_time(session, parent_sample_id)
        touch_resource_modification_time(session, child_sample_id)
        session.commit()
        return {"message": f"Successfully unlinked {parent_sample_id} -> {child_sample_id}"}


@router.patch('/samples/{sample_id}', response_model=SampleResponse)
def update_sample(auth_res: Annotated[Principal, Depends(authorize_user)], sample_id: str, updated_sample: SampleUpdate,
                  background_tasks: BackgroundTasks):
    '''
    Partially update a sample record. Only provided fields are modified.
    No project_id — use POST /resources/{mfid}/project.
    '''
    user_unique_id = auth_res.user_id

    with Session(engine) as session:
        sample = session.exec(select(Sample).where(Sample.unique_id == sample_id)).one_or_none()
        if not sample:
            raise HTTPException(status_code=404, detail='Sample not found')

        require_permission(session, auth_res, sample_id, Permission.EDIT)

        update_dict = updated_sample.model_dump(exclude_unset=True)
        new_public = update_dict.pop('public', None)
        for key, value in update_dict.items():
            setattr(sample, key, value)

        ri = session.exec(select(Resource).where(Resource.mfid == sample_id)).one_or_none()
        if ri:
            ri.modification_time = get_utc_isoformat()
            if new_public is not None:
                ri.public = new_public
                set_public(session, sample_id, bool(new_public))

        session.add(sample)
        session.commit()
        session.refresh(sample)
        background_tasks.add_task(write_record, sample.unique_id, sample_record(sample, ri), user_unique_id, "update")
        project_references = resolve_project_mfids_to_references([sample.project_mfid], session)
        project_reference = project_references.get(sample.project_mfid)
        return SampleResponse.model_validate({
            **sample.model_dump(),
            "project_id": project_reference.project_id if project_reference else sample.project_id,
            "project": project_reference,
            "public": ri.public if ri else False,
            "creation_time": ri.creation_time if ri else None,
            "modification_time": ri.modification_time if ri else None,
        })


@router.post("/samples", response_model=SampleResponse)
def create_sample(auth_res: Annotated[Principal, Depends(authorize_user)], S: SampleCreate,
                  background_tasks: BackgroundTasks):
    '''
    Create a new sample. Sample names are not unique within a project, so repeating one
    creates a second sample.
    '''
    user_unique_id = auth_res.user_id

    if not S.sample_name.strip():
        raise HTTPException(status_code=400, detail="sample_name must not be empty.")

    sample_details = S.model_dump(exclude_unset=True)
    with Session(engine) as session:
        if S.unique_id:
            assert_mfid_available(session, S.unique_id, "sample")

        owner_user = resolve_create_owner(
            session,
            sample_details.get('owner'),
            sample_details.get('owner_orcid'),
            user_unique_id,
        )
        resolved_owner_orcid = owner_user.unique_id

        project_mfid = None
        if S.project_id:
            project_mfid = resolve_project_id_to_mfid(S.project_id, session)
            if project_mfid is None:
                raise HTTPException(status_code=404, detail=f"Project '{S.project_id}' not found")

        if not auth_res.is_platform_admin:
            owner_orcid = resolved_owner_orcid or user_unique_id
            if S.project_id:
                if owner_orcid != user_unique_id and not auth_res.is_service_account:
                    raise HTTPException(
                        status_code=403,
                        detail="Only service accounts and platform administrators may create a sample on behalf of another user")
                standing = project_standing(owner_orcid, S.project_id, session)
                if standing is None or standing < Role.CONTRIBUTOR:
                    raise HTTPException(
                        status_code=403,
                        detail=f"'{owner_orcid}' must be contributor or above in project '{S.project_id}' to create there")
            elif owner_orcid != user_unique_id:
                if not auth_res.is_service_account:
                    raise HTTPException(
                        status_code=400,
                        detail="Creating on behalf of another user requires project_id, to verify their standing there")

        sample_details.pop('owner', None)
        sample_details['owner_orcid'] = resolved_owner_orcid
        sample_details['owner_user_id'] = owner_user.id
        if project_mfid is not None:
            sample_details['project_mfid'] = project_mfid

        sample = Sample(**sample_details)
        session.add(sample)
        session.flush()

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
        set_public(session, sample.unique_id, bool(S.public))

        create_standard_acls(session, sample.unique_id,
                             owner_id=sample.owner_orcid,
                             requestor_id=user_unique_id,
                             project_id=sample.project_id)

        session.add(sample)
        session.commit()
        session.refresh(sample)
        background_tasks.add_task(write_record, sample.unique_id, sample_record(sample, ri), user_unique_id, "create")
        project_references = resolve_project_mfids_to_references([sample.project_mfid], session)
        project_reference = project_references.get(sample.project_mfid)
        return SampleResponse.model_validate({
            **sample.model_dump(),
            "project_id": project_reference.project_id if project_reference else sample.project_id,
            "project": project_reference,
            "public": ri.public if ri else False,
            "creation_time": ri.creation_time if ri else None,
            "modification_time": ri.modification_time if ri else None,
        })


@router.get("/samples", response_model=PaginatedResponse[SampleResponse])
def list_samples(auth_res: Annotated[Principal, Depends(authorize_user)], request: Request,
                 accessible_to_user: Optional[List[str]] = Query(default=None, description="User references whose effective access must all include each result."),
                 accessible_to_project: Optional[List[str]] = Query(default=None, description="Project MFIDs or slugs whose direct access must all include each result."),
                 include_metadata: bool = False,
                 include_links: bool = False,
                 include_datasets: bool = False,
                 include_owner: bool = False,
                 limit: int = Query(default=100, ge=1, le=1000),
                 cursor: Optional[str] = None,
                 offset: int = Query(default=0, ge=0),
                 creation_time_gte: Optional[str] = None, creation_time_lte: Optional[str] = None,
                 modification_time_gte: Optional[str] = None, modification_time_lte: Optional[str] = None,
                 visibility: Literal["all", "public", "private"] = "all",
                 dataset_mfid: Annotated[Optional[str], Query(
                     description="Filter samples linked to this canonical dataset MFID.",
                 )] = None):
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
    include_datasets is deprecated; use include_links instead.
    Pass visibility=public or visibility=private to narrow by the resource's public ACL entry.
    Pass dataset_mfid to filter by a readable dataset's canonical stable identity.
    Access selectors use AND semantics and only narrow resources readable by the caller.
    '''
    import warnings
    if include_datasets:
        warnings.warn("include_datasets is deprecated, use include_links=true instead", DeprecationWarning)
        include_links = True


    params_dict = dict(request.query_params)
    for k in ('creation_time_gte', 'creation_time_lte', 'modification_time_gte',
              'modification_time_lte', 'include_metadata', 'include_links', 'include_datasets',
              'include_owner', 'limit', 'cursor', 'offset', 'visibility',
              'accessible_to_user', 'accessible_to_project', 'dataset_mfid'):
        params_dict.pop(k, None)

    with Session(engine) as session:
        linked_dataset_id = None
        if dataset_mfid is not None:
            linked_dataset_id = session.exec(
                select(Dataset.id)
                .where(Dataset.unique_id == dataset_mfid)
                .where(permission_filter(auth_res, Dataset, Permission.READ))
            ).one_or_none()
            if linked_dataset_id is None:
                raise HTTPException(status_code=404, detail="Dataset not found or access denied")

        auth_filter = access_selector_filter(
            session,
            auth_res,
            Sample,
            user_refs=accessible_to_user,
            project_refs=accessible_to_project,
        )
        deletion_filter = deletion_filter_for(Sample)

        def apply_filters(q):
            q = q.where(auth_filter).where(deletion_filter)
            if visibility != "all":
                q = q.where(visibility_filter_for(Sample, visibility))
            if creation_time_gte:
                q = q.where(Resource.creation_time >= creation_time_gte)
            if creation_time_lte:
                q = q.where(Resource.creation_time <= creation_time_lte)
            if modification_time_gte:
                q = q.where(Resource.modification_time >= modification_time_gte)
            if modification_time_lte:
                q = q.where(Resource.modification_time <= modification_time_lte)
            if linked_dataset_id is not None:
                q = q.where(
                    exists()
                    .where(
                        DatasetSampleLink.dataset_id == linked_dataset_id,
                        DatasetSampleLink.sample_id == Sample.id,
                    )
                    .correlate(Sample)
                )
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
                select(Sample, Resource.creation_time, Resource.modification_time, Resource.public,
                       ScientificMetadataBinary.scientific_metadata, Project.unique_id,
                       Project.project_id, Project.title)
                .join(Resource, Resource.mfid == Sample.unique_id, isouter=True)
                .join(ScientificMetadataBinary, ScientificMetadataBinary.unique_id == Sample.unique_id, isouter=True)
                .outerjoin(Project, Project.unique_id == Sample.project_mfid)
            )
        else:
            samp_cmd = (
                select(Sample, Resource.creation_time, Resource.modification_time, Resource.public,
                       Project.unique_id, Project.project_id, Project.title)
                .join(Resource, Resource.mfid == Sample.unique_id, isouter=True)
                .outerjoin(Project, Project.unique_id == Sample.project_mfid)
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
                for child_id, parent, relationship_type in session.exec(
                    select(SampleLink.sample_id, Sample, SampleLink.relationship_type)
                    .join(Sample, Sample.id == SampleLink.parent_sample_id)
                    .where(SampleLink.sample_id.in_(sample_ids))
                    .where(permission_filter(auth_res, Sample, Permission.READ))
                    .where(deletion_filter_for(Sample))
                    .order_by(SampleLink.sample_id, Sample.unique_id)
                ).all():
                    parents_by_sample.setdefault(child_id, []).append(
                        LinkedResource(unique_id=parent.unique_id, resource_type="sample", name=parent.sample_name, direction="source", relationship_type=relationship_type)
                    )
                for parent_id, child, relationship_type in session.exec(
                    select(SampleLink.parent_sample_id, Sample, SampleLink.relationship_type)
                    .join(Sample, Sample.id == SampleLink.sample_id)
                    .where(SampleLink.parent_sample_id.in_(sample_ids))
                    .where(permission_filter(auth_res, Sample, Permission.READ))
                    .where(deletion_filter_for(Sample))
                    .order_by(SampleLink.parent_sample_id, Sample.unique_id)
                ).all():
                    children_by_sample.setdefault(parent_id, []).append(
                        LinkedResource(unique_id=child.unique_id, resource_type="sample", name=child.sample_name, direction="target", relationship_type=relationship_type)
                    )
                sample_dataset_rows = session.exec(
                    select(DatasetSampleLink.sample_id, Dataset)
                    .join(Dataset, Dataset.id == DatasetSampleLink.dataset_id)
                    .where(DatasetSampleLink.sample_id.in_(sample_ids))
                    .where(permission_filter(auth_res, Dataset, Permission.READ))
                    .where(deletion_filter_for(Dataset))
                    .order_by(DatasetSampleLink.sample_id, Dataset.unique_id)
                ).all()
                instrument_references = resolve_instrument_mfids_to_references(
                    [ds.instrument_mfid for _, ds in sample_dataset_rows], session)
                ds_project_references = resolve_project_mfids_to_references(
                    [ds.project_mfid for _, ds in sample_dataset_rows], session)
                for sid, ds in sample_dataset_rows:
                    datasets_by_sample.setdefault(sid, []).append(
                        LinkedResource(unique_id=ds.unique_id, resource_type="dataset", name=ds.dataset_name, direction="undirected")
                    )
                    dataset_reads_by_sample.setdefault(sid, []).append(DatasetRead.model_validate({
                        **ds.model_dump(),
                        "instrument_id": instrument_references[ds.instrument_mfid].instrument_id if ds.instrument_mfid in instrument_references else None,
                        "project_id": ds_project_references[ds.project_mfid].project_id if ds.project_mfid in ds_project_references else ds.project_id,
                        "instrument": instrument_references.get(ds.instrument_mfid),
                        "project": ds_project_references.get(ds.project_mfid),
                    }))

        # Batch-resolve owners if requested
        owners_by_orcid = {}
        if include_owner:
            orcids = {row[0].owner_orcid for row in rows if row[0].owner_orcid}
            if orcids:
                for u in session.exec(select(User).where(User.unique_id.in_(orcids))).all():
                    owners_by_orcid[u.unique_id] = UserPublicRead.model_validate(u)

        items = []
        for row in rows:
            if include_metadata:
                samp, creation_time, modification_time, public, sci_md, project_mfid, resolved_project_id, project_title = row
            else:
                samp, creation_time, modification_time, public, project_mfid, resolved_project_id, project_title = row
                sci_md = None
            project_reference = ProjectReferenceRead(
                unique_id=project_mfid,
                project_id=resolved_project_id,
                title=project_title,
            ) if project_mfid is not None else None
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
                "project_id": resolved_project_id if resolved_project_id is not None else samp.project_id,
                "project": project_reference,
                "public": public if public is not None else False,
                "creation_time": creation_time,
                "modification_time": modification_time,
                "scientific_metadata": sci_md,
                "datasets": compat_datasets,
                "links": links,
                "owner": owners_by_orcid.get(samp.owner_orcid),
            }))
        next_cursor = rows[-1][0].unique_id if len(rows) == limit else None
        resp_offset = offset if (cursor is None and offset) else None
        return PaginatedResponse(total=total, limit=limit, offset=resp_offset, items=items, next_cursor=next_cursor)


def _sample_base_query(principal):
    """Auth-scoped, deletion-filtered base query for samples. Caller adds
    ordering and pagination. Joins Project directly so callers get the
    live-resolved project_id alongside each row, rather than a second
    resolve query after the fact."""
    auth_filter = permission_filter(principal, Sample)
    deletion_filter = deletion_filter_for(Sample)
    return (
        select(Sample, Resource.creation_time, Resource.modification_time, Resource.public,
               Project.unique_id, Project.project_id, Project.title)
        .join(Resource, Resource.mfid == Sample.unique_id, isouter=True)
        .outerjoin(Project, Project.unique_id == Sample.project_mfid)
        .where(auth_filter)
        .where(deletion_filter)
    )


@router.get("/samples/search", response_model=PaginatedResponse[SampleResponse])
def search_samples(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    q: str = Query(min_length=3, description="Search term matched against sample_name."),
    project_id: Optional[str] = None,
    limit: int = Query(default=20, ge=1, le=50),
):
    '''
    Search samples by name. Returns results ordered by match quality (exact → prefix → substring).
    Optionally scope to a project with ?project_id=. Maximum 50 results.
    '''

    with Session(engine) as session:
        session.execute(text(f"SET LOCAL pg_trgm.word_similarity_threshold = {get_trgm_search_threshold()}"))
        query = _sample_base_query(auth_res)
        for token in q.lower().split():
            query = query.where(
                Sample.sample_name.ilike(f"{token}%") |
                literal(token).op("<%")(Sample.sample_name)
            )
        if project_id:
            query = query.where(Sample.project_mfid == resolve_project_id_to_mfid(project_id, session))
        query = query.order_by(func.word_similarity(q, Sample.sample_name).desc()).limit(limit)
        results = session.exec(query).all()
        items = [SampleResponse.model_validate({
            **samp.model_dump(),
            "project_id": resolved_project_id if resolved_project_id is not None else samp.project_id,
            "project": ProjectReferenceRead(
                unique_id=project_mfid,
                project_id=resolved_project_id,
                title=project_title,
            ) if project_mfid is not None else None,
            "public": public if public is not None else False,
            "creation_time": creation_time,
            "modification_time": modification_time,
        }) for samp, creation_time, modification_time, public, project_mfid, resolved_project_id, project_title in results]
        return PaginatedResponse(limit=limit, items=items)


@router.get("/samples/{sample_mfid}", response_model=SampleResponse)
def read_sample(
    sample_mfid: str,
    auth_res: Annotated[Principal, Depends(authorize_user)],
    include_links: bool = False,
    include_metadata: bool = False,
    include_owner: bool = True,
    include_datasets: bool = Query(
        default=True,
        description="Include legacy embedded dataset objects. Set false and use include_links or GET /datasets?sample_mfid=... for efficient relationship access.",
    ),
):
    '''
    Returns the sample for the provided MFID.
    Pass include_links=true to include parent, child, and associated resources.
    Embedded dataset objects are retained by default for compatibility. New clients should pass include_datasets=false and use include_links or GET /datasets?sample_mfid=... instead.
    The owner identity is resolved by default. Pass include_owner=false to omit it.
    '''
    with Session(engine) as session:
        return load_sample_detail(
            session,
            auth_res,
            sample_mfid,
            options=ResourceDetailOptions(
                include_metadata=include_metadata,
                include_links=include_links,
                include_owner=include_owner,
                include_datasets=include_datasets,
            ),
        )


@router.get("/samples/{sample_id}/datasets", deprecated=True)
def list_sample_datasets(
    sample_id: str,
    auth_res: Annotated[Principal, Depends(authorize_user)],
    request: Request,
    response: Response,
    include_metadata: bool = False,
    limit: Optional[int] = None,
):
    '''
    Returns a list of all datasets linked to a given sample.
    Deprecated: use GET /datasets?sample_mfid={sample_id}.
    '''
    response.headers["Deprecation"] = RELATIONSHIP_ROUTE_DEPRECATION

    params_dict = dict(request.query_params)
    keyword_filter = params_dict.pop("keyword", None)

    with Session(engine) as session:
        # Verify sample exists
        sample = session.exec(select(Sample).where(Sample.unique_id == sample_id)).one_or_none()

        if sample is None:
            raise HTTPException(status_code=404, detail="Sample not found")
        require_permission(session, auth_res, sample_id, Permission.READ)

        auth_filter = permission_filter(auth_res, Dataset, Permission.READ)
        deletion_filter = deletion_filter_for(Dataset)
        if include_metadata:
            query = (
                    select(Dataset, ScientificMetadataBinary.scientific_metadata)
                    .join(ScientificMetadataBinary, ScientificMetadataBinary.unique_id == Dataset.unique_id, isouter=True)
                    .filter(auth_filter)
                    .where(deletion_filter)
                )

        else:
            query = select(Dataset).filter(auth_filter).where(deletion_filter)

        if keyword_filter:
            query = generate_kw_query(session, query, keyword_filter)

        for key, value in params_dict.items():
            if hasattr(Dataset, key):
                query = query.where(getattr(Dataset, key) == value)

        full_query = query.join(DatasetSampleLink,
                                DatasetSampleLink.dataset_id == Dataset.id
                                ).where(
                                    DatasetSampleLink.sample_id == sample.id
                                ).distinct()

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
def list_parent_samples(child_sample_id: str, auth_res: Annotated[Principal, Depends(authorize_user)],
                        limit: int = Query(default=100, ge=1, le=1000),
                        offset: int = Query(default=0, ge=0),
                        relationship_type: Optional[Literal["is_derived_from", "is_part_of"]] = Query(default=None)):
    '''
    Returns a paginated list of parent samples for a given child sample.

    relationship_type restricts the result to links of that kind. Links with no
    recorded type are excluded when the filter is set.
    '''
    with Session(engine) as session:
        child_sample = session.exec(select(Sample).where(Sample.unique_id == child_sample_id)).one_or_none()
        if child_sample is None:
            raise HTTPException(status_code=404, detail="Child sample not found")
        require_permission(session, auth_res, child_sample_id, Permission.READ)

        query = (
            select(Sample)
            .select_from(join(Sample, SampleLink, SampleLink.parent_sample_id == Sample.id))
            .where(SampleLink.sample_id == child_sample.id)
            .where(permission_filter(auth_res, Sample, Permission.READ))
            .where(deletion_filter_for(Sample))
        )
        if relationship_type is not None:
            query = query.where(SampleLink.relationship_type == relationship_type)
        total = session.exec(select(func.count()).select_from(query.subquery())).one()
        rows = session.exec(query.offset(offset).limit(limit)).all()
        project_references = resolve_project_mfids_to_references((s.project_mfid for s in rows), session)
        items = [SampleRead.model_validate({
            **s.model_dump(),
            "project_id": project_references[s.project_mfid].project_id if s.project_mfid in project_references else s.project_id,
            "project": project_references.get(s.project_mfid),
        }) for s in rows]
        return PaginatedResponse(total=total, limit=limit, offset=offset, items=items)


@router.get("/samples/{parent_sample_id}/children", response_model=PaginatedResponse[SampleRead])
def list_child_samples(parent_sample_id: str, auth_res: Annotated[Principal, Depends(authorize_user)],
                       limit: int = Query(default=100, ge=1, le=1000),
                       offset: int = Query(default=0, ge=0),
                       relationship_type: Optional[Literal["is_derived_from", "is_part_of"]] = Query(default=None)):
    '''
    Returns a paginated list of child samples for a given parent sample.

    relationship_type restricts the result to links of that kind. Links with no
    recorded type are excluded when the filter is set.
    '''
    with Session(engine) as session:
        parent_sample = session.exec(select(Sample).where(Sample.unique_id == parent_sample_id)).one_or_none()
        if parent_sample is None:
            raise HTTPException(status_code=404, detail="Parent sample not found")
        require_permission(session, auth_res, parent_sample_id, Permission.READ)

        query = (
            select(Sample)
            .select_from(join(Sample, SampleLink, SampleLink.sample_id == Sample.id))
            .where(SampleLink.parent_sample_id == parent_sample.id)
            .where(permission_filter(auth_res, Sample, Permission.READ))
            .where(deletion_filter_for(Sample))
        )
        if relationship_type is not None:
            query = query.where(SampleLink.relationship_type == relationship_type)
        total = session.exec(select(func.count()).select_from(query.subquery())).one()
        rows = session.exec(query.offset(offset).limit(limit)).all()
        project_references = resolve_project_mfids_to_references((s.project_mfid for s in rows), session)
        items = [SampleRead.model_validate({
            **s.model_dump(),
            "project_id": project_references[s.project_mfid].project_id if s.project_mfid in project_references else s.project_id,
            "project": project_references.get(s.project_mfid),
        }) for s in rows]
        return PaginatedResponse(total=total, limit=limit, offset=offset, items=items)

@router.get("/samples/{sample_id}/sample_graph")
def sample_graph(sample_id: str, auth_res: Annotated[Principal, Depends(authorize_user)]):
    '''
    Returns a NetworkX node-link graph of all samples connected (via parent/child
    relationships) to the given sample. Loads all samples in the project and
    returns the directed lineage subgraph (ancestors + descendants) of the target.
    '''

    with Session(engine) as session:
        target = session.exec(
            select(Sample).where(Sample.unique_id == sample_id)
        ).one_or_none()

        if target is None:
            raise HTTPException(status_code=404, detail="Sample not found")

        require_permission(session, auth_res, sample_id, Permission.READ)

        samp_list = session.exec(
            select(Sample)
            .where(permission_filter(auth_res, Sample, Permission.READ))
            .where(deletion_filter_for(Sample))
            .where(Sample.project_mfid == target.project_mfid)
        ).unique().all()

        # Read edges from the link table rather than walking Sample.parents, so
        # each edge can carry its relationship_type. Restricting both endpoints
        # to samp_list yields the same edge set the relationship walk did.
        id_to_uid = {sample.id: sample.unique_id for sample in samp_list}
        node_ids = list(id_to_uid.keys())
        edge_rows = session.exec(
            select(SampleLink.parent_sample_id, SampleLink.sample_id, SampleLink.relationship_type)
            .where(SampleLink.parent_sample_id.in_(node_ids))
            .where(SampleLink.sample_id.in_(node_ids))
        ).all() if node_ids else []

        edges = [(id_to_uid[r[0]], id_to_uid[r[1]], {"relationship_type": r[2]}) for r in edge_rows]

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
def sample_graph_cte(sample_id: str, auth_res: Annotated[Principal, Depends(authorize_user)]):
    '''
    CTE-based implementation (kept for reference). Prefer /sample_graph.
    Returns a NetworkX node-link graph of all samples connected (via parent/child
    relationships) to the given sample, i.e. its full lineage subgraph.
    '''

    with Session(engine) as session:
        target = session.exec(
            select(Sample).where(Sample.unique_id == sample_id)
        ).one_or_none()

        if target is None:
            raise HTTPException(status_code=404, detail="Sample not found")

        require_permission(session, auth_res, sample_id, Permission.READ)

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

        reachable_ids = [row.unique_id for row in rows]
        allowed_ids = set(session.exec(
            select(Sample.unique_id)
            .where(Sample.unique_id.in_(reachable_ids))
            .where(permission_filter(auth_res, Sample, Permission.READ))
            .where(deletion_filter_for(Sample))
        ).all())
        rows = [row for row in rows if row.unique_id in allowed_ids]

        id_to_uid = {r.id: r.unique_id for r in rows}
        node_ids = list(id_to_uid.keys())

        # Fetch all edges whose both endpoints are within the component
        edge_rows = session.exec(
            select(SampleLink.parent_sample_id, SampleLink.sample_id, SampleLink.relationship_type)
            .where(SampleLink.parent_sample_id.in_(node_ids))
            .where(SampleLink.sample_id.in_(node_ids))
        ).all()

        G = nx.DiGraph((id_to_uid[r[0]], id_to_uid[r[1]], {"relationship_type": r[2]}) for r in edge_rows)
        for r in rows:
            G.add_node(r.unique_id, sample_name=r.sample_name)

        return json_graph.node_link_data(G)

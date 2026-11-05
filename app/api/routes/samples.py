from typing import Annotated, Literal, Optional
import logging

logger = logging.getLogger(__name__)

import os
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response
from sqlalchemy import exists, func, join
from sqlmodel import and_, Session, select

from app.api.dependencies import get_engine, get_trgm_search_threshold
from app.api.path_params import ChildSampleMfid, ParentSampleMfid, SampleMfid
from app.general_utils import get_utc_datetime
from app.models.core_models import (
    Dataset, DatasetSampleLink,
    Project, Resource, Sample, SampleLink, ScientificMetadataBinary, User,
)
from app.models.datasets import DatasetRead
from app.models.facets import FacetResponse
from app.models.links import LinkedResource
from app.models.pagination import PaginatedResponse
from app.models.query_params import (
    UNIQUE_ID_FILTER_DEPRECATION, ResourceSearchQuery, SampleDetailQuery, SampleFacetQuery, SampleListQuery)
from app.models.projects import ProjectReferenceRead, ProjectScope
from app.models.samples import SampleCreate, SampleRead, SampleResponse, SampleUpdate

from app.utils.auth_utils import (
    authorize_user, permission_filter, project_scope_filter,
    require_permission, resource_affiliation_filter)
from app.utils.instrument_utils import dataset_instrument_fields, resolve_instrument_mfids_to_references
from app.utils.link_utils import reject_lineage_cycle, reject_self_link, require_link_create_permissions
from app.utils.permissions import Principal, Permission, Role, has_global_permission
from app.utils.project_utils import (
    project_not_found,
    project_standing_by_mfid,
    resolve_project_filter_mfid,
    resolve_project_identifiers,
    resolve_project_mfids_to_references,
)
from app.utils.search_utils import (
    apply_trigram_search,
    configure_trigram_search,
    normalize_search_query,
    trigram_rank,
)
from app.utils.sql_utils import is_public, public_grant_exists, add_or_update, assert_mfid_available, assert_parent_accepts_assignment, deletion_filter_for, select_active, create_standard_acls, touch_resource_modification_time, set_public

from app.utils.gcs_record_utils import write_record, sample_record
from app.utils.user_utils import resolve_create_owner
from app.models.users import UserPublicRead
from app.models.response_schemas import MessageResponse
from app.services.resource_details import ResourceDetailOptions, load_sample_detail
from app.services.resource_facets import list_sample_facets
from app.services.resource_filters import resource_scope_predicates
from app.services.resource_pagination import (
    ResourcePaginationSpec,
    execute_resource_page,
    prepare_resource_pagination,
)

router = APIRouter(tags=["samples"])
SAMPLE_PAGINATION = ResourcePaginationSpec(
    kind="sample",
    unique_id_column=Sample.unique_id,
    sorts={
        "created": Resource.creation_time,
        "updated": func.coalesce(Resource.modification_time, Resource.creation_time),
        "name": func.lower(Sample.sample_name),
    },
)

API_VERSION = os.getenv('API_VERSION', None)
API_VERSION_GITHASH = os.getenv("GITHASH", None)

@router.post("/samples/{parent_sample_mfid}/children/{child_sample_mfid}", response_model=SampleLink)
def link_sample_parent_child(parent_sample_id: ParentSampleMfid, child_sample_id: ChildSampleMfid, auth_res: Annotated[Principal, Depends(authorize_user)],
                             relationship_type: Optional[Literal["is_derived_from", "is_part_of"]] = Query(default=None)):
    '''
    Link two samples as parent and child.

    relationship_type describes the child relative to the parent, so
    is_part_of means "child is_part_of parent". Omit it to leave the kind
    unspecified; on a link that already exists, omitting it preserves the
    type already stored rather than clearing it.
    Same-project links require contributor access to the parent and read access
    to the child. Cross-project links and relationship-type changes require
    editor access to both samples.
    '''

    reject_self_link(parent_sample_id, child_sample_id)
    with Session(get_engine()) as session:
        parent_sample = session.exec(select(Sample).where(Sample.unique_id == parent_sample_id)).first()
        if not parent_sample:
            raise HTTPException(status_code=404, detail="Parent sample not found")
        child_sample = session.exec(select(Sample).where(Sample.unique_id == child_sample_id)).first()
        if not child_sample:
            raise HTTPException(status_code=404, detail="Child sample not found")

        require_link_create_permissions(session, auth_res, parent_sample, child_sample)

        link = session.exec(
            select(SampleLink).where(
                SampleLink.parent_sample_id == parent_sample.id,
                SampleLink.sample_id == child_sample.id,
            )
        ).one_or_none()
        if link is not None:
            if relationship_type is None or link.relationship_type == relationship_type:
                return link
            require_permission(session, auth_res, parent_sample_id, Permission.EDIT)
            require_permission(session, auth_res, child_sample_id, Permission.EDIT)
            link.relationship_type = relationship_type
        else:
            reject_lineage_cycle(session, SampleLink.parent_sample_id, SampleLink.sample_id, parent_sample.id, child_sample.id)
            link = SampleLink(
                parent_sample_id=parent_sample.id,
                sample_id=child_sample.id,
                relationship_type=relationship_type,
            )
        session.add(link)

        touch_resource_modification_time(session, parent_sample_id)
        touch_resource_modification_time(session, child_sample_id)

        session.commit()
        session.refresh(link)
        return link


@router.delete("/samples/{parent_sample_mfid}/children/{child_sample_mfid}", response_model=MessageResponse)
def unlink_sample_parent_child(parent_sample_id: ParentSampleMfid, child_sample_id: ChildSampleMfid, auth_res: Annotated[Principal, Depends(authorize_user)]):

    with Session(get_engine()) as session:
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


@router.patch('/samples/{sample_mfid}', response_model=SampleResponse)
def update_sample(auth_res: Annotated[Principal, Depends(authorize_user)], sample_id: SampleMfid, updated_sample: SampleUpdate,
                  background_tasks: BackgroundTasks):
    '''
    Partially update a sample record. Only provided fields are modified.
    No project_id — use POST /resources/{mfid}/project.
    '''
    user_unique_id = auth_res.user_id

    with Session(get_engine()) as session:
        sample = session.exec(select(Sample).where(Sample.unique_id == sample_id)).one_or_none()
        if not sample:
            raise HTTPException(status_code=404, detail='Sample not found')

        require_permission(session, auth_res, sample_id, Permission.EDIT)

        update_dict = updated_sample.model_dump(exclude_unset=True)
        for key, value in update_dict.items():
            setattr(sample, key, value)

        ri = session.exec(select(Resource).where(Resource.mfid == sample_id)).one_or_none()
        if ri:
            ri.modification_time = get_utc_datetime()

        session.add(sample)
        session.commit()
        session.refresh(sample)
        public = is_public(session, sample.unique_id)
        background_tasks.add_task(write_record, sample.unique_id, sample_record(sample, ri, public), user_unique_id, "update")
        project_references = resolve_project_mfids_to_references([sample.project_mfid], session)
        project_reference = project_references.get(sample.project_mfid)
        return SampleResponse.model_validate({
            **sample.model_dump(),
            "project_id": project_reference.project_id if project_reference else None,
            "project": project_reference,
            "public": public,
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
    with Session(get_engine()) as session:
        if S.unique_id:
            assert_mfid_available(session, S.unique_id, "sample")

        owner_user = resolve_create_owner(
            session,
            sample_details.get('owner'),
            sample_details.get('owner_orcid'),
            user_unique_id,
        )
        resolved_owner_orcid = owner_user.unique_id

        requested_project_id = sample_details.get('project_id')
        requested_project_mfid = sample_details.get('project_mfid')
        project = resolve_project_identifiers(
            session,
            requested_project_id,
            requested_project_mfid,
        )
        if project is None and (
            requested_project_id is not None
            or requested_project_mfid is not None
        ):
            raise project_not_found(requested_project_id, requested_project_mfid)

        if not has_global_permission(auth_res, Permission.CREATE):
            if (resolved_owner_orcid or user_unique_id) != user_unique_id:
                raise HTTPException(
                    status_code=403,
                    detail="Only platform contributors and administrators may create a sample on behalf of another user")
            if project is not None:
                standing = project_standing_by_mfid(user_unique_id, project.unique_id, session)
                if standing is None or standing < Role.CONTRIBUTOR:
                    raise HTTPException(
                        status_code=403,
                        detail=f"Caller '{user_unique_id}' must be contributor or above in project '{project.project_id}' to create there")

        sample_details.pop('owner', None)
        sample_details['owner_orcid'] = resolved_owner_orcid
        sample_details['owner_user_id'] = owner_user.id
        if project is not None:
            assert_parent_accepts_assignment(session, project.unique_id)
            sample_details['project_mfid'] = project.unique_id

        sample = Sample(**sample_details)
        session.add(sample)
        session.flush()

        now = get_utc_datetime()
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
        set_public(session, sample.unique_id, bool(S.public))

        create_standard_acls(session, sample.unique_id,
                             owner_id=sample.owner_orcid,
                             requestor_id=user_unique_id,
                             project_mfid=sample.project_mfid)

        session.add(sample)
        session.commit()
        session.refresh(sample)
        public = is_public(session, sample.unique_id)
        background_tasks.add_task(write_record, sample.unique_id, sample_record(sample, ri, public), user_unique_id, "create")
        project_references = resolve_project_mfids_to_references([sample.project_mfid], session)
        project_reference = project_references.get(sample.project_mfid)
        return SampleResponse.model_validate({
            **sample.model_dump(),
            "project_id": project_reference.project_id if project_reference else None,
            "project": project_reference,
            "public": public,
            "creation_time": ri.creation_time if ri else None,
            "modification_time": ri.modification_time if ri else None,
        })


@router.get("/samples/facets", response_model=FacetResponse)
def get_sample_facets(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    params: Annotated[SampleFacetQuery, Query()],
):
    with Session(get_engine()) as session:
        return list_sample_facets(session, auth_res, params)


@router.get("/samples", response_model=PaginatedResponse[SampleResponse])
def list_samples(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    request: Request,
    response: Response,
    params: Annotated[SampleListQuery, Query()],
):
    '''
    Returns a paginated list of samples the requesting user is authorized to view.
    Default limit is 100, max is 1000.

    By default, results are ordered by unique_id descending. To fetch a keyset page,
    pass cursor=<next_cursor from the previous response>. The first page omits cursor.
    next_cursor is null when there are no further pages. total is included only when
    cursor is omitted and offset is zero. Pass include_total=true to request it on
    any page or include_total=false to suppress it.

    Offset pagination is supported for backward compatibility: pass offset=<n> instead
    of cursor. When both are supplied, cursor takes precedence. Prefer cursor for large
    result sets, since deep offsets are slower.
    Pass affiliation=owner to return only resources authoritatively owned by the caller.
    Use owner_id for an exact owner match; owner_orcid remains as a deprecated alias.
    Pass sort=created, updated, or name with direction=asc or desc for explicit ordering.
    Pass anchor_mfid without cursor or offset to start an inclusive page at a resource
    within the authorized, filtered, and ordered collection.
    Pass include_links=true to include parent, child, and associated resources for each sample.
    include_datasets is deprecated; use include_links instead.
    Pass visibility=public or visibility=private to narrow by the resource's public ACL entry.
    Pass project_id or project_mfid with project_scope=assigned, shared, or all to filter by project relationship.
    Pass dataset_mfid to filter by a readable dataset's canonical stable identity.
    Faceted nullable fields also accept field-specific is_null=true or false filters.
    Access selectors use AND semantics and only narrow resources readable by the caller.
    '''
    import warnings

    include_metadata = params.include_metadata
    include_links = params.include_links
    if "unique_id" in request.query_params:
        response.headers["Deprecation"] = UNIQUE_ID_FILTER_DEPRECATION
    param_values = params.model_dump()
    include_datasets = param_values["include_datasets"]
    include_owner = params.include_owner
    include_total = params.include_total
    project_id = params.project_id
    project_mfid = params.project_mfid
    dataset_mfid = params.dataset_mfid
    if include_datasets:
        warnings.warn("include_datasets is deprecated, use include_links=true instead", DeprecationWarning)
        include_links = True

    exact_filters = {
        key: param_values[key]
        for key in (
            "public", "sample_name", "sample_type",
            "description", "timestamp",
        )
        if param_values[key] is not None
    }
    owner_id = params.owner_id or param_values["owner_orcid"]
    if owner_id is not None:
        exact_filters["owner_orcid"] = owner_id
    result_filters = params.normalized_result_filters(
        exclude={
            "anchor_mfid", "cursor", "offset", "limit", "sort", "direction",
            "include_metadata", "include_links", "include_datasets", "include_owner",
            "include_total", "owner_orcid",
        },
    )
    result_filters["owner_id"] = owner_id
    pagination = prepare_resource_pagination(
        params=params,
        query_params=request.query_params,
        spec=SAMPLE_PAGINATION,
        result_filters=result_filters,
        user_id=auth_res.user_id,
    )

    with Session(get_engine()) as session:
        filter_project_mfid = resolve_project_filter_mfid(
            session, project_id, project_mfid)
        linked_dataset_id = None
        if dataset_mfid is not None:
            linked_dataset_id = session.exec(
                select_active(Dataset, dataset_mfid, Dataset.id, principal=auth_res)
            ).one_or_none()
            if linked_dataset_id is None:
                raise HTTPException(status_code=404, detail="Dataset not found or access denied")

        scope_predicates = resource_scope_predicates(
            session,
            auth_res,
            Sample,
            params,
            filter_project_mfid,
        )

        def apply_filters(q):
            q = q.where(*scope_predicates)
            if linked_dataset_id is not None:
                q = q.where(
                    exists()
                    .where(
                        DatasetSampleLink.dataset_id == linked_dataset_id,
                        DatasetSampleLink.sample_id == Sample.id,
                    )
                    .correlate(Sample)
                )
            if params.mfid is not None:
                q = q.where(Sample.unique_id.in_(params.mfid))
            for key, value in exact_filters.items():
                q = q.where(getattr(Sample, key) == value)
            for column, value in (
                (Sample.sample_type, params.sample_type_is_null),
                (Sample.owner_orcid, params.owner_id_is_null),
                (Sample.project_mfid, params.project_mfid_is_null),
            ):
                if value is not None:
                    q = q.where(column.is_(None) if value else column.is_not(None))
            return q

        total = None
        if pagination.should_include_total(include_total):
            total = session.exec(apply_filters(
                select(func.count()).select_from(Sample)
                .join(Resource, Resource.mfid == Sample.unique_id, isouter=True)
            )).one()

        if include_metadata:
            samp_cmd = (
                select(Sample, Resource.creation_time, Resource.modification_time, public_grant_exists(Sample.unique_id),
                       ScientificMetadataBinary.scientific_metadata, Project.unique_id,
                       Project.project_id, Project.title)
                .join(Resource, Resource.mfid == Sample.unique_id, isouter=True)
                .join(ScientificMetadataBinary, ScientificMetadataBinary.unique_id == Sample.unique_id, isouter=True)
                .outerjoin(Project, Project.unique_id == Sample.project_mfid)
            )
        else:
            samp_cmd = (
                select(Sample, Resource.creation_time, Resource.modification_time, public_grant_exists(Sample.unique_id),
                       Project.unique_id, Project.project_id, Project.title)
                .join(Resource, Resource.mfid == Sample.unique_id, isouter=True)
                .outerjoin(Project, Project.unique_id == Sample.project_mfid)
            )

        samp_cmd = apply_filters(samp_cmd)
        page = execute_resource_page(session, samp_cmd, pagination)
        rows = page.rows

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
                        **dataset_instrument_fields(ds, instrument_references.get(ds.instrument_mfid)),
                        "project_id": ds_project_references[ds.project_mfid].project_id if ds.project_mfid in ds_project_references else None,
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
                samp, creation_time, modification_time, public, sci_md, resolved_project_mfid, resolved_project_id, project_title = row
            else:
                samp, creation_time, modification_time, public, resolved_project_mfid, resolved_project_id, project_title = row
                sci_md = None
            project_reference = ProjectReferenceRead(
                unique_id=resolved_project_mfid,
                project_id=resolved_project_id,
                title=project_title,
            ) if resolved_project_mfid is not None else None
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
                "project_id": resolved_project_id,
                "project": project_reference,
                "public": public,
                "creation_time": creation_time,
                "modification_time": modification_time,
                "scientific_metadata": sci_md,
                "datasets": compat_datasets,
                "links": links,
                "owner": owners_by_orcid.get(samp.owner_orcid),
                "project_relation": (
                    "assigned" if samp.project_mfid == filter_project_mfid else "shared"
                ) if filter_project_mfid is not None else None,
            }))
        return PaginatedResponse(
            total=total,
            limit=pagination.limit,
            offset=pagination.response_offset,
            items=items,
            next_cursor=page.next_cursor,
        )


def _sample_base_query(principal):
    """Auth-scoped, deletion-filtered base query for samples. Caller adds
    ordering and pagination. Joins Project directly so callers get the
    live-resolved project_id alongside each row, rather than a second
    resolve query after the fact."""
    auth_filter = permission_filter(principal, Sample)
    deletion_filter = deletion_filter_for(Sample)
    return (
        select(Sample, Resource.creation_time, Resource.modification_time, public_grant_exists(Sample.unique_id),
               Project.unique_id, Project.project_id, Project.title)
        .join(Resource, Resource.mfid == Sample.unique_id, isouter=True)
        .outerjoin(Project, Project.unique_id == Sample.project_mfid)
        .where(auth_filter)
        .where(deletion_filter)
    )


@router.get("/samples/search", response_model=PaginatedResponse[SampleResponse])
def search_samples(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    params: Annotated[ResourceSearchQuery, Query()],
):
    '''
    Search readable samples by name and return results ordered by match quality.
    Use owner_id for an exact owner match.
    Pass project_id or project_mfid with project_scope=assigned, shared, or all to filter by project relationship. Maximum 50 results.
    '''

    q = params.q
    project_id = params.project_id
    project_mfid = params.project_mfid
    project_scope = params.project_scope
    limit = params.limit
    effective_project_scope: ProjectScope = project_scope or "assigned"
    normalized_q, tokens = normalize_search_query(q)

    with Session(get_engine()) as session:
        configure_trigram_search(session, get_trgm_search_threshold())
        filter_project_mfid = resolve_project_filter_mfid(
            session, project_id, project_mfid)
        query = apply_trigram_search(
            _sample_base_query(auth_res),
            tokens,
            fields=(Sample.sample_name,),
            prefix_fields=(Sample.sample_name,),
        )
        if filter_project_mfid is not None:
            query = query.where(project_scope_filter(
                session,
                auth_res,
                Sample,
                filter_project_mfid,
                effective_project_scope,
            ))
        if params.affiliation:
            query = query.where(resource_affiliation_filter(auth_res, Sample, params.affiliation))
        if params.owner_id is not None:
            query = query.where(Sample.owner_orcid == params.owner_id)
        query = query.order_by(
            trigram_rank(normalized_q, (Sample.sample_name,)).desc(),
            Sample.unique_id.desc(),
        ).limit(limit)
        results = session.exec(query).all()
        items = [SampleResponse.model_validate({
            **samp.model_dump(),
            "project_id": resolved_project_id,
            "project": ProjectReferenceRead(
                unique_id=project_mfid,
                project_id=resolved_project_id,
                title=project_title,
            ) if project_mfid is not None else None,
            "public": public,
            "creation_time": creation_time,
            "modification_time": modification_time,
            "project_relation": (
                "assigned" if samp.project_mfid == filter_project_mfid else "shared"
            ) if filter_project_mfid is not None else None,
        }) for samp, creation_time, modification_time, public, project_mfid, resolved_project_id, project_title in results]
        return PaginatedResponse(limit=limit, items=items)


@router.get("/samples/{sample_mfid}", response_model=SampleResponse)
def read_sample(
    sample_mfid: str,
    auth_res: Annotated[Principal, Depends(authorize_user)],
    params: Annotated[SampleDetailQuery, Query()],
):
    '''
    Returns the sample for the provided MFID.
    Pass include_links=true to include parent, child, and associated resources.
    Embedded dataset objects are retained by default for compatibility. New clients should pass include_datasets=false and use include_links or GET /datasets?sample_mfid=... instead.
    The owner identity is resolved by default. Pass include_owner=false to omit it.
    '''
    with Session(get_engine()) as session:
        return load_sample_detail(
            session,
            auth_res,
            sample_mfid,
            options=ResourceDetailOptions(
                include_metadata=params.include_metadata,
                include_links=params.include_links,
                include_owner=params.include_owner,
                include_datasets=params.include_datasets,
            ),
        )


@router.get("/samples/{child_sample_mfid}/parents", response_model=PaginatedResponse[SampleRead])
def list_parent_samples(child_sample_id: ChildSampleMfid, auth_res: Annotated[Principal, Depends(authorize_user)],
                        limit: int = Query(default=100, ge=1, le=1000),
                        offset: int = Query(default=0, ge=0),
                        relationship_type: Optional[Literal["is_derived_from", "is_part_of"]] = Query(default=None)):
    '''
    Returns a paginated list of parent samples for a given child sample.

    relationship_type restricts the result to links of that kind. Links with no
    recorded type are excluded when the filter is set.
    '''
    with Session(get_engine()) as session:
        child_sample = session.exec(select_active(Sample, child_sample_id)).one_or_none()
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
            "project_id": project_references[s.project_mfid].project_id if s.project_mfid in project_references else None,
            "project": project_references.get(s.project_mfid),
        }) for s in rows]
        return PaginatedResponse(total=total, limit=limit, offset=offset, items=items)


@router.get("/samples/{parent_sample_mfid}/children", response_model=PaginatedResponse[SampleRead])
def list_child_samples(parent_sample_id: ParentSampleMfid, auth_res: Annotated[Principal, Depends(authorize_user)],
                       limit: int = Query(default=100, ge=1, le=1000),
                       offset: int = Query(default=0, ge=0),
                       relationship_type: Optional[Literal["is_derived_from", "is_part_of"]] = Query(default=None)):
    '''
    Returns a paginated list of child samples for a given parent sample.

    relationship_type restricts the result to links of that kind. Links with no
    recorded type are excluded when the filter is set.
    '''
    with Session(get_engine()) as session:
        parent_sample = session.exec(select_active(Sample, parent_sample_id)).one_or_none()
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
            "project_id": project_references[s.project_mfid].project_id if s.project_mfid in project_references else None,
            "project": project_references.get(s.project_mfid),
        }) for s in rows]
        return PaginatedResponse(total=total, limit=limit, offset=offset, items=items)


import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated, Dict, List, Literal, Optional

logger = logging.getLogger(__name__)

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Query, Request, Response
from sqlalchemy import exists, func, join
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from sqlmodel import and_, Session, select

from app.services.related_records import dataset_responses
from app.services.resource_access import record_roles
from app.services.instrument_members import grant_dataset as grant_instrument_members, revoke_dataset as revoke_instrument_members
from app.api.dependencies import get_engine, get_trgm_search_threshold
from app.api.path_params import ChildDatasetMfid, DatasetMfid, ParentDatasetMfid, SampleMfid
from app.api.errors import STORAGE_NOT_CONFIGURED_RESPONSE, feature_disabled_response
from app.api.removed import REMOVED_RESPONSE, removed
from app.general_utils import get_utc_datetime
from app.tables import (
    AccessGroup,
    AssociatedFile,
    Dataset,
    DatasetKeywordLink,
    DatasetLink,
    DatasetSampleLink,
    Instrument,
    Keyword,
    Project,
    Resource,
    ResourceACL,
    Sample,
    ScientificMetadataBinary,
    Thumbnail,
    User,
    UserAccessGroupLink,
)
from app.models.response_schemas import DatasetSampleLinkResponse, MessageResponse
from app.models.facets import FacetResponse
from app.models.datasets import (
    KeywordRead,
    AssociatedFileCreate, AssociatedFileRead,
    DatasetCreate, DatasetInstrumentAssign, DatasetInstrumentRead,
    DatasetResponse, DatasetUpdate, file_etag,
    ThumbnailCreate, ThumbnailPreviewRead, ThumbnailRead, ThumbnailUpdate,
)
from app.models.instrument import InstrumentReferenceRead
from app.models.links import DatasetLinkRead, LinkedResource
from app.models.pagination import PaginatedResponse
from app.models.query_params import (
    UNIQUE_ID_FILTER_DEPRECATION, DatasetDetailQuery, DatasetFacetQuery, DatasetListQuery, DatasetSearchQuery)
from app.models.projects import ProjectReferenceRead, ProjectScope
from app.services import multipart
from app.services.resource_details import ResourceDetailOptions, load_dataset_detail
from app.services.resource_facets import list_dataset_facets
from app.services.resource_filters import resource_scope_predicates
from app.services.resource_pagination import (
    ResourcePaginationSpec,
    execute_resource_page,
    prepare_resource_pagination,
)
from app.services.thumbnails import InvalidThumbnailImage, ThumbnailTooLarge, normalize_thumbnail
from app.utils.auth_utils import (
    authorize_user, permission_filter, project_scope_filter,
    require_permission, resource_affiliation_filter)
from app.utils.permissions import (
    AccessGroupType, Permission, Principal, Role, has_global_permission)
from app.utils.project_utils import (
    project_not_found,
    project_standing_by_mfid,
    resolve_project_filter_mfid,
    resolve_project_identifiers,
    resolve_project_mfids_to_references,
)
from app.utils.instrument_utils import (
    dataset_instrument_fields,
    resolve_instrument_filter_mfid,
    resolve_instrument_identifiers,
    resolve_instrument_mfids_to_references,
)
from app.utils.link_utils import reject_lineage_cycle, reject_self_link, require_link_create_permissions
from app.api.routes.pipelines import start_postprocessing_run
from app.models.pipelines import PostprocessingRunRead
from app.utils.search_utils import (
    apply_trigram_search,
    configure_trigram_search,
    normalize_search_query,
    trigram_rank,
)
from app.models.query_params import requested_owners
from app.utils.user_utils import owned_by_any, owner_fields, owner_unique_id, owners_of, resolve_create_owner
from app.utils.sql_utils import record_resource, is_public, public_grant_exists, add_or_update, assert_mfid_available, assert_not_pending_deletion, assert_parent_accepts_assignment, select_active, generate_kw_query, sql_create_resource_acl, create_standard_acls, touch_resource_modification_time, set_public, deletion_filter_for
from app.utils.gcs_record_utils import write_record, dataset_record
from app.api.routes.gcs_storage import _ensure_storage_path, _signed_download

router = APIRouter(tags=["datasets"])
DOWNLOAD_SIGNING_WORKERS = 16
DATASET_PAGINATION = ResourcePaginationSpec(
    kind="dataset",
    unique_id_column=Dataset.unique_id,
    sorts={
        "created": Resource.creation_time,
        "updated": func.coalesce(Resource.modification_time, Resource.creation_time),
        "name": func.lower(Dataset.dataset_name),
    },
)
LARGE_THUMBNAIL_LOOKUP_WARNING_SIZE = int(
    os.environ.get("THUMBNAIL_LARGE_LOOKUP_WARNING_SIZE", "1000")
)


def _normalize_thumbnail_input(dsid: str, thumbnail_b64str: str) -> str:
    try:
        return normalize_thumbnail(thumbnail_b64str)
    except ThumbnailTooLarge as exc:
        raise HTTPException(status_code=413, detail="Thumbnail image is too large") from exc
    except InvalidThumbnailImage as exc:
        raise HTTPException(status_code=400, detail="Invalid thumbnail image") from exc
    except Exception as exc:
        logger.exception("Unexpected thumbnail processing failure for dataset %s", dsid)
        raise HTTPException(status_code=500, detail="Unable to process thumbnail image") from exc


@router.post("/datasets/{dataset_mfid}/carrier_segmentation", response_model=PostprocessingRunRead, deprecated=True, responses=feature_disabled_response("ingestion"))
def request_carrier_segmentation(dsid: DatasetMfid, auth_res: Annotated[Principal, Depends(authorize_user)]):
    '''Deprecated: use POST /datasets/{dataset_mfid}/postprocess with pipeline "carrier_segmentation".'''
    return start_postprocessing_run(auth_res, dsid, "carrier_segmentation", {})


@router.post("/datasets/{dataset_mfid}/insitu_spec_aggregation", response_model=PostprocessingRunRead, deprecated=True, responses=feature_disabled_response("ingestion"))
def request_insitu_aggregation(dsid: DatasetMfid, auth_res: Annotated[Principal, Depends(authorize_user)]):
    '''Deprecated: use POST /datasets/{dataset_mfid}/postprocess with pipeline "insitu_aggregation".'''
    return start_postprocessing_run(auth_res, dsid, "insitu_aggregation", {})


@router.post("/datasets/{dataset_mfid}/rga_analysis", response_model=PostprocessingRunRead, deprecated=True, responses=feature_disabled_response("ingestion"))
def request_rga_analysis(dsid: DatasetMfid, auth_res: Annotated[Principal, Depends(authorize_user)]):
    '''Deprecated: use POST /datasets/{dataset_mfid}/postprocess with pipeline "rga_analysis".'''
    return start_postprocessing_run(auth_res, dsid, "rga_analysis", {})


@router.post("/datasets/{dataset_mfid}/mosaic_stitch", response_model=PostprocessingRunRead, deprecated=True, responses=feature_disabled_response("ingestion"))
def request_mosaic_stitch(dsid: DatasetMfid, auth_res: Annotated[Principal, Depends(authorize_user)]):
    '''Deprecated: use POST /datasets/{dataset_mfid}/postprocess with pipeline "mosaic_stitch".'''
    return start_postprocessing_run(auth_res, dsid, "mosaic_stitch", {})


@router.post("/datasets", response_model=DatasetResponse)
def create_dataset(auth_res: Annotated[Principal, Depends(authorize_user)],
                   dataset: DatasetCreate,
                   background_tasks: BackgroundTasks):
    
    logger.info(f"Creating dataset: {dataset.unique_id}")
    dataset_details = dataset.model_dump(exclude_unset=True)
    user_unique_id = auth_res.user_id
    with Session(get_engine()) as session:
        if dataset_details.get('unique_id'):
            assert_mfid_available(session, dataset_details['unique_id'], "dataset")

        owner_user = resolve_create_owner(
            session,
            dataset_details.pop('owner', None),
            dataset_details.get('owner_orcid'),
            user_unique_id,
        )
        dataset_details['owner_orcid'] = owner_user.unique_id
        dataset_details['owner_user_id'] = owner_user.id

        requested_project_id = dataset_details.get('project_id')
        requested_project_mfid = dataset_details.get('project_mfid')
        project = resolve_project_identifiers(
            session,
            requested_project_id,
            requested_project_mfid,
        )
        if project is None and (requested_project_id is not None or requested_project_mfid is not None):
            raise project_not_found(requested_project_id, requested_project_mfid)

        if not has_global_permission(auth_res, Permission.CREATE):
            if not auth_res.is_user(owner_user):
                raise HTTPException(
                    status_code=403,
                    detail="Only platform contributors and administrators may create a dataset on behalf of another user")
            if project is not None:
                standing = project_standing_by_mfid(auth_res.internal_user_id, project.unique_id, session)
                if standing is None or standing < Role.CONTRIBUTOR:
                    raise HTTPException(
                        status_code=403,
                        detail=f"Caller '{user_unique_id}' must be contributor or above in project '{project.project_id}' to create there")

        if project is not None:
            assert_parent_accepts_assignment(session, project.unique_id)
            dataset_details['project_mfid'] = project.unique_id

        instrument_id_slug = dataset_details.pop('instrument_id', None)
        instrument_mfid = dataset_details.pop('instrument_mfid', None)
        instrument = resolve_instrument_identifiers(
            session,
            instrument_id_slug,
            instrument_mfid,
        )
        if instrument is not None:
            assert_parent_accepts_assignment(session, instrument.unique_id)
            dataset_details['instrument_mfid'] = instrument.unique_id

        ds = Dataset(**dataset_details)
        session.add(ds)
        session.flush()

        ri = record_resource(session, ds.unique_id, "dataset")
        set_public(session, ds.unique_id, bool(ds.public))
        
        session.flush()

        create_standard_acls(session, ds.unique_id,
                             owner_id=dataset_details.get('owner_orcid'),
                             requestor_id=user_unique_id,
                             project_mfid=dataset_details.get('project_mfid'))

        if instrument is not None:
            inst_ag = session.exec(select(AccessGroup).where(
                AccessGroup.group_name == instrument.unique_id,
                AccessGroup.group_type == AccessGroupType.INSTRUMENT.value,
            )).one_or_none()
            if inst_ag is None:
                raise HTTPException(
                    status_code=500,
                    detail=f"Instrument '{instrument.instrument_name}' has no operator group provisioned")
            sql_create_resource_acl(inst_ag, ds.unique_id, read=True, write=True,
                                    session=session, permission=Role.CONTRIBUTOR)
            grant_instrument_members(session, ds.unique_id, instrument.unique_id)

        session.commit()
        session.refresh(ds)
        is_bound = True
        if instrument is not None and auth_res.is_service_account:
            is_bound = session.exec(
                select(UserAccessGroupLink.user_id)
                .join(User, User.id == UserAccessGroupLink.user_id)
                .join(AccessGroup, AccessGroup.id == UserAccessGroupLink.accessgroup_id)
                .where(
                    User.unique_id == user_unique_id,
                    AccessGroup.group_name == instrument.unique_id,
                    AccessGroup.group_type == AccessGroupType.INSTRUMENT.value,
                )
            ).first() is not None
        if not is_bound:
            logger.warning(
                "Unbound service account %s created dataset %s for instrument %s (%s)",
                user_unique_id,
                ds.unique_id,
                instrument.instrument_id,
                instrument.unique_id,
            )
        public = is_public(session, ds.unique_id)
        background_tasks.add_task(write_record, ds.unique_id, dataset_record(ds, ri, public, owner_unique_id(session, ds)), user_unique_id, "create")
        project_references = resolve_project_mfids_to_references([ds.project_mfid], session)
        return DatasetResponse.model_validate({
            **ds.model_dump(),
            **owner_fields(ds, {owner_user.id: owner_user}),
            **dataset_instrument_fields(ds, InstrumentReferenceRead.model_validate(instrument) if instrument else None),
            "project_id": project_references[ds.project_mfid].project_id if ds.project_mfid in project_references else None,
            "project": project_references.get(ds.project_mfid),
            "public": public,
            "creation_time": ri.creation_time,
            "modification_time": ri.modification_time,
        })


@router.patch("/datasets/{dataset_mfid}", response_model=DatasetResponse)
def update_dataset(auth_res: Annotated[Principal, Depends(authorize_user)], dsid: DatasetMfid, updated_dataset: DatasetUpdate,
                   background_tasks: BackgroundTasks):
    '''
    Partially update a dataset record. Only provided fields are modified.
    No owner_orcid/project_id — use POST /resources/{mfid}/transfer_ownership
    and POST /resources/{mfid}/project.
    '''
    user_unique_id = auth_res.user_id

    with Session(get_engine()) as session:
        ds = session.exec(select(Dataset).where(Dataset.unique_id == dsid)).one_or_none()
        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found")

        require_permission(session, auth_res, dsid, Permission.EDIT)

        update_dict = updated_dataset.model_dump(exclude_unset=True)
        instrument_updates = {
            field: update_dict.pop(field)
            for field in ("instrument_id",)
            if field in update_dict
        }
        if instrument_updates:
            current_instrument = None
            if ds.instrument_mfid is not None:
                current_instrument = session.exec(
                    select(Instrument).where(Instrument.unique_id == ds.instrument_mfid)
                ).one_or_none()

            current_instrument_id = current_instrument.instrument_id if current_instrument else None
            requested_instrument_id = instrument_updates.get("instrument_id", current_instrument_id)
            id_matches = (
                requested_instrument_id is None and current_instrument_id is None
            ) or (
                isinstance(requested_instrument_id, str)
                and isinstance(current_instrument_id, str)
                and requested_instrument_id.casefold() == current_instrument_id.casefold()
            )
            if not id_matches:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Dataset instrument assignment is not editable here. "
                        "Use PUT /datasets/{dataset_mfid}/instrument, which keeps the "
                        "instrument columns and the resource ACL in step."
                    ),
                )

        upds = add_or_update(session, Dataset, Dataset.unique_id == dsid, **update_dict)

        ri = session.exec(select(Resource).where(Resource.mfid == dsid)).one_or_none()
        if ri:
            ri.modification_time = get_utc_datetime()

        session.commit()
        session.refresh(upds)
        public = is_public(session, upds.unique_id)
        background_tasks.add_task(write_record, upds.unique_id, dataset_record(upds, ri, public, owner_unique_id(session, upds)), user_unique_id, "update")
        instrument_references = resolve_instrument_mfids_to_references([upds.instrument_mfid], session)
        project_references = resolve_project_mfids_to_references([upds.project_mfid], session)
        instrument_reference = instrument_references.get(upds.instrument_mfid)
        project_reference = project_references.get(upds.project_mfid)
        return DatasetResponse.model_validate({
            **upds.model_dump(),
            **owner_fields(upds, owners_of(session, [upds])),
            **dataset_instrument_fields(upds, instrument_reference),
            "project_id": project_reference.project_id if project_reference else None,
            "project": project_reference,
            "public": public,
            "creation_time": ri.creation_time if ri else None,
            "modification_time": ri.modification_time if ri else None,
        })


@router.put("/datasets/{dataset_mfid}/instrument", response_model=DatasetInstrumentRead)
def assign_dataset_instrument(auth_res: Annotated[Principal, Depends(authorize_user)], dsid: DatasetMfid,
                              assignment: DatasetInstrumentAssign,
                              background_tasks: BackgroundTasks):
    '''
    Assign or reassign the dataset's instrument, by slug or MFID.

    Only instrument_mfid is stored; the instrument's slug and name in responses
    are read from the instrument row.

    The instrument's access group is granted contributor on the dataset, the
    same grant dataset creation makes. Reassignment removes the grant belonging
    to the outgoing instrument's group only — resourceacl records no provenance,
    so a broader sweep could not tell a derived grant from a deliberate one.

    Resubmitting the dataset's current instrument changes no columns but still
    ensures the ACL row exists, which repairs datasets whose instrument columns
    were backfilled without the accompanying grant.
    '''
    user_unique_id = auth_res.user_id

    with Session(get_engine()) as session:
        ds = session.exec(select(Dataset).where(Dataset.unique_id == dsid)).one_or_none()
        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found")

        require_permission(session, auth_res, dsid, Permission.MANAGE_ACL)

        instrument = resolve_instrument_identifiers(
            session, assignment.instrument_id, assignment.instrument_mfid)
        assert_parent_accepts_assignment(session, instrument.unique_id)

        previous_instrument = None
        if ds.instrument_mfid is not None:
            previous_instrument = session.exec(
                select(Instrument).where(Instrument.unique_id == ds.instrument_mfid)
            ).one_or_none()
        # Built before the commit expires these rows, so the response never
        # depends on reloading an instrument mid-serialization.
        previous_reference = (
            InstrumentReferenceRead.model_validate(previous_instrument)
            if previous_instrument is not None else None
        )
        new_reference = InstrumentReferenceRead.model_validate(instrument)

        new_ag = session.exec(
            select(AccessGroup).where(
                AccessGroup.group_name == instrument.unique_id,
                AccessGroup.group_type == AccessGroupType.INSTRUMENT.value,
            )
        ).one_or_none()
        if new_ag is None:
            raise HTTPException(
                status_code=409,
                detail=f"Instrument '{instrument.unique_id}' has no access group provisioned")

        if previous_instrument is not None and previous_instrument.unique_id != instrument.unique_id:
            old_ag = session.exec(
                select(AccessGroup).where(
                    AccessGroup.group_name == previous_instrument.unique_id,
                    AccessGroup.group_type == AccessGroupType.INSTRUMENT.value,
                )
            ).one_or_none()
            if old_ag is not None:
                stale = session.exec(
                    select(ResourceACL).where(
                        ResourceACL.mfid == dsid,
                        ResourceACL.accessgroup_id == old_ag.id,
                    )
                ).one_or_none()
                if stale is not None:
                    session.delete(stale)
                    session.flush()
            revoke_instrument_members(session, dsid, previous_instrument.unique_id)

        sql_create_resource_acl(new_ag, dsid, read=True, write=True,
                                session=session, permission=Role.CONTRIBUTOR)
        grant_instrument_members(session, dsid, instrument.unique_id)

        ds.instrument_mfid = instrument.unique_id
        session.add(ds)

        ri = touch_resource_modification_time(session, dsid)
        session.commit()
        session.refresh(ds)

        background_tasks.add_task(write_record, ds.unique_id, dataset_record(ds, ri, is_public(session, ds.unique_id), owner_unique_id(session, ds)),
                                  user_unique_id, "update")
        return DatasetInstrumentRead(
            dataset_mfid=dsid,
            previous_instrument=previous_reference,
            previous_instrument_name=previous_reference.instrument_name if previous_reference else None,
            instrument=new_reference,
        )
@router.get("/datasets/facets", response_model=FacetResponse)
def get_dataset_facets(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    params: Annotated[DatasetFacetQuery, Query()],
):
    with Session(get_engine()) as session:
        return list_dataset_facets(session, auth_res, params)


@router.get("/datasets", response_model=PaginatedResponse[DatasetResponse])
def list_datasets(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    request: Request,
    response: Response,
    params: Annotated[DatasetListQuery, Query()],
):
    '''
    Returns a paginated list of datasets the requesting user is authorized to view.
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
    Use owner_ref, repeatable, for an exact owner match; owner_id and owner_orcid are deprecated.
    Pass sort=created, updated, or name with direction=asc or desc for explicit ordering.
    Pass anchor_mfid without cursor or offset to start an inclusive page at a resource
    within the authorized, filtered, and ordered collection.
    Pass include_links=true to include parent, child, and associated resources for each dataset.
    Pass visibility=public or visibility=private to narrow by the resource's public ACL entry.
    Pass instrument_mfid to filter by the instrument's canonical stable identity.
    Pass project_id or project_mfid with project_scope=assigned, shared, or all to filter by project relationship.
    Pass sample_mfid to filter by a readable sample's canonical stable identity.
    Faceted nullable fields also accept field-specific is_null=true or false filters.
    Access selectors use AND semantics and only narrow resources readable by the caller.
    '''
    include_metadata = params.include_metadata
    include_links = params.include_links
    include_owner = params.include_owner
    include_total = params.include_total
    instrument_mfid = params.instrument_mfid
    project_id = params.project_id
    project_mfid = params.project_mfid
    sample_mfid = params.sample_mfid
    keyword_filter = params.keyword
    if "unique_id" in request.query_params:
        response.headers["Deprecation"] = UNIQUE_ID_FILTER_DEPRECATION
    param_values = params.model_dump()
    exact_filters = {
        key: param_values[key]
        for key in (
            "dataset_name",
            "timestamp", "size", "data_format",
            "data_type", "measurement", "session_name",
        )
        if param_values[key] is not None
    }
    owners = requested_owners(params, param_values["owner_orcid"])
    result_filters = params.normalized_result_filters(
        exclude={
            "anchor_mfid", "cursor", "offset", "limit", "sort", "direction",
            "include_metadata", "include_links", "include_owner", "include_total",
            "owner_orcid", "owner_ref",
        },
    )
    result_filters["owner_id"] = owners
    pagination = prepare_resource_pagination(
        params=params,
        query_params=request.query_params,
        spec=DATASET_PAGINATION,
        result_filters=result_filters,
        user_id=auth_res.user_id,
    )

    with Session(get_engine()) as session:
        filter_project_mfid = resolve_project_filter_mfid(
            session, project_id, project_mfid)
        instrument_mfid = resolve_instrument_filter_mfid(
            session, params.instrument_id, instrument_mfid)
        linked_sample_id = None
        if sample_mfid is not None:
            linked_sample_id = session.exec(select_active(Sample, sample_mfid, Sample.id, principal=auth_res)).one_or_none()
            if linked_sample_id is None:
                raise HTTPException(status_code=404, detail="Sample not found or access denied")

        scope_predicates = resource_scope_predicates(
            session,
            auth_res,
            Dataset,
            params,
            filter_project_mfid,
        )

        # Base filters shared between count and data queries
        def apply_filters(q):
            q = q.where(*scope_predicates)
            if instrument_mfid is not None:
                q = q.where(Dataset.instrument_mfid == instrument_mfid)
            if linked_sample_id is not None:
                q = q.where(
                    exists()
                    .where(
                        DatasetSampleLink.sample_id == linked_sample_id,
                        DatasetSampleLink.dataset_id == Dataset.id,
                    )
                    .correlate(Dataset)
                )
            if params.mfid is not None:
                q = q.where(Dataset.unique_id.in_(params.mfid))
            if owners:
                q = q.where(owned_by_any(session, Dataset.owner_user_id, owners))
            if params.public is not None:
                q = q.where(public_grant_exists(Dataset.unique_id) if params.public else ~public_grant_exists(Dataset.unique_id))
            for key, value in exact_filters.items():
                q = q.where(getattr(Dataset, key) == value)
            for column, value in (
                (Dataset.session_name, params.session_name_is_null),
                (Dataset.measurement, params.measurement_is_null),
                (Dataset.data_format, params.data_format_is_null),
                (Dataset.owner_user_id, params.owner_is_null),
                (Dataset.instrument_mfid, params.instrument_mfid_is_null),
                (Dataset.project_mfid, params.project_mfid_is_null),
            ):
                if value is not None:
                    q = q.where(column.is_(None) if value else column.is_not(None))
            return q

        total = None
        if pagination.should_include_total(include_total):
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
                       public_grant_exists(Dataset.unique_id), ScientificMetadataBinary.scientific_metadata,
                       Project.unique_id, Project.project_id, Project.title,
                       Instrument.unique_id, Instrument.instrument_id, Instrument.instrument_name)
                .join(Resource, Resource.mfid == Dataset.unique_id, isouter=True)
                .join(ScientificMetadataBinary, ScientificMetadataBinary.unique_id == Dataset.unique_id, isouter=True)
                .outerjoin(Project, Project.unique_id == Dataset.project_mfid)
                .outerjoin(Instrument, Instrument.unique_id == Dataset.instrument_mfid)
            )
        else:
            query = (
                select(Dataset, Resource.creation_time, Resource.modification_time, public_grant_exists(Dataset.unique_id),
                       Project.unique_id, Project.project_id, Project.title,
                       Instrument.unique_id, Instrument.instrument_id, Instrument.instrument_name)
                .join(Resource, Resource.mfid == Dataset.unique_id, isouter=True)
                .outerjoin(Project, Project.unique_id == Dataset.project_mfid)
                .outerjoin(Instrument, Instrument.unique_id == Dataset.instrument_mfid)
            )

        query = apply_filters(query)
        if keyword_filter:
            query = generate_kw_query(session, query, keyword_filter)
        page = execute_resource_page(session, query, pagination)
        results = page.rows

        if include_links:
            dataset_ids = [row[0].id for row in results]
            parents_by_dataset: dict = {}
            children_by_dataset: dict = {}
            samples_by_dataset: dict = {}
            if dataset_ids:
                for child_id, parent, relationship_type in session.exec(
                    select(DatasetLink.child_dataset_id, Dataset, DatasetLink.relationship_type)
                    .join(Dataset, Dataset.id == DatasetLink.parent_dataset_id)
                    .where(DatasetLink.child_dataset_id.in_(dataset_ids))
                    .where(permission_filter(auth_res, Dataset, Permission.READ))
                    .where(deletion_filter_for(Dataset))
                    .order_by(DatasetLink.child_dataset_id, Dataset.unique_id)
                ).all():
                    parents_by_dataset.setdefault(child_id, []).append(
                        LinkedResource(unique_id=parent.unique_id, resource_type="dataset", name=parent.dataset_name, direction="source", relationship_type=relationship_type)
                    )
                for parent_id, child, relationship_type in session.exec(
                    select(DatasetLink.parent_dataset_id, Dataset, DatasetLink.relationship_type)
                    .join(Dataset, Dataset.id == DatasetLink.child_dataset_id)
                    .where(DatasetLink.parent_dataset_id.in_(dataset_ids))
                    .where(permission_filter(auth_res, Dataset, Permission.READ))
                    .where(deletion_filter_for(Dataset))
                    .order_by(DatasetLink.parent_dataset_id, Dataset.unique_id)
                ).all():
                    children_by_dataset.setdefault(parent_id, []).append(
                        LinkedResource(unique_id=child.unique_id, resource_type="dataset", name=child.dataset_name, direction="target", relationship_type=relationship_type)
                    )
                for ds_id, samp in session.exec(
                    select(DatasetSampleLink.dataset_id, Sample)
                    .join(Sample, Sample.id == DatasetSampleLink.sample_id)
                    .where(DatasetSampleLink.dataset_id.in_(dataset_ids))
                    .where(permission_filter(auth_res, Sample, Permission.READ))
                    .where(deletion_filter_for(Sample))
                    .order_by(DatasetSampleLink.dataset_id, Sample.unique_id)
                ).all():
                    samples_by_dataset.setdefault(ds_id, []).append(
                        LinkedResource(unique_id=samp.unique_id, resource_type="sample", name=samp.sample_name, direction="undirected")
                    )

        owners = owners_of(session, (row[0] for row in results))
        roles = record_roles(session, auth_res, [row[0].unique_id for row in results])

        items = []
        for row in results:
            if include_metadata:
                dataset, creation_time, modification_time, public, sci_md, resolved_project_mfid, project_id, project_title, instrument_mfid, instrument_id, instrument_name = row
            else:
                dataset, creation_time, modification_time, public, resolved_project_mfid, project_id, project_title, instrument_mfid, instrument_id, instrument_name = row
                sci_md = None
            instrument_reference = InstrumentReferenceRead(
                unique_id=instrument_mfid,
                instrument_id=instrument_id,
                instrument_name=instrument_name,
            ) if instrument_mfid else None
            project_reference = ProjectReferenceRead(
                unique_id=resolved_project_mfid,
                project_id=project_id,
                title=project_title,
            ) if resolved_project_mfid else None
            links = None
            if include_links:
                links = (
                    parents_by_dataset.get(dataset.id, []) +
                    children_by_dataset.get(dataset.id, []) +
                    samples_by_dataset.get(dataset.id, [])
                )
            items.append(DatasetResponse.model_validate({
                **dataset.model_dump(),
                **dataset_instrument_fields(dataset, instrument_reference),
                "project_id": project_id,
                "project": project_reference,
                "public": public,
                "creation_time": creation_time,
                "modification_time": modification_time,
                "scientific_metadata": sci_md,
                "links": links,
                **owner_fields(dataset, owners, include_owner),
                "role": roles.get(dataset.unique_id),
                "project_relation": (
                    "assigned" if dataset.project_mfid == filter_project_mfid else "shared"
                ) if filter_project_mfid is not None else None,
            }))
        return PaginatedResponse(
            total=total,
            limit=pagination.limit,
            offset=pagination.response_offset,
            items=items,
            next_cursor=page.next_cursor,
        )


def _dataset_base_query(principal):
    """Auth-scoped, deletion-filtered base query for datasets. Caller adds
    ordering and pagination. Joins Project/Instrument directly so callers get
    live project and instrument references alongside each row."""
    auth_filter = permission_filter(principal, Dataset)
    deletion_filter = deletion_filter_for(Dataset)
    return (
        select(Dataset, Resource.creation_time, Resource.modification_time, public_grant_exists(Dataset.unique_id),
               Project.unique_id, Project.project_id, Project.title,
               Instrument.unique_id, Instrument.instrument_id, Instrument.instrument_name)
        .join(Resource, Resource.mfid == Dataset.unique_id, isouter=True)
        .outerjoin(Project, Project.unique_id == Dataset.project_mfid)
        .outerjoin(Instrument, Instrument.unique_id == Dataset.instrument_mfid)
        .where(auth_filter)
        .where(deletion_filter)
    )


@router.get("/datasets/search", response_model=PaginatedResponse[DatasetResponse])
def search_datasets(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    params: Annotated[DatasetSearchQuery, Query()],
):
    '''
    Search readable datasets by name and return results ordered by match quality.
    Use owner_ref, repeatable, for an exact owner match.
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
            _dataset_base_query(auth_res),
            tokens,
            fields=(Dataset.dataset_name,),
            prefix_fields=(Dataset.dataset_name,),
        )
        if filter_project_mfid is not None:
            query = query.where(project_scope_filter(
                session,
                auth_res,
                Dataset,
                filter_project_mfid,
                effective_project_scope,
            ))
        if params.affiliation:
            query = query.where(resource_affiliation_filter(auth_res, Dataset, params.affiliation))
        if owners := requested_owners(params):
            query = query.where(owned_by_any(session, Dataset.owner_user_id, owners))
        query = query.order_by(
            trigram_rank(normalized_q, (Dataset.dataset_name,)).desc(),
            Dataset.unique_id.desc(),
        ).limit(limit)
        results = session.exec(query).all()
        owners = owners_of(session, (row[0] for row in results))
        roles = record_roles(session, auth_res, [row[0].unique_id for row in results])
        items = [DatasetResponse.model_validate({
            **dataset.model_dump(),
            **owner_fields(dataset, owners, params.include_owner),
            "role": roles.get(dataset.unique_id),
            **dataset_instrument_fields(dataset, InstrumentReferenceRead(
                unique_id=instrument_mfid,
                instrument_id=instrument_id,
                instrument_name=instrument_name,
            ) if instrument_mfid else None),
            "project_id": resolved_project_id,
            "project": ProjectReferenceRead(
                unique_id=resolved_project_mfid,
                project_id=resolved_project_id,
                title=project_title,
            ) if resolved_project_mfid else None,
            "public": public,
            "creation_time": creation_time,
            "modification_time": modification_time,
            "project_relation": (
                "assigned" if dataset.project_mfid == filter_project_mfid else "shared"
            ) if filter_project_mfid is not None else None,
        }) for dataset, creation_time, modification_time, public, resolved_project_mfid, resolved_project_id, project_title, instrument_mfid, instrument_id, instrument_name in results]
        return PaginatedResponse(limit=limit, items=items)


@router.get("/datasets/{dataset_mfid}", response_model=DatasetResponse)
def read_dataset(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    dataset_mfid: str,
    params: Annotated[DatasetDetailQuery, Query()],
):
    '''
    Returns the dataset with the provided MFID.
    Pass include_links=true to include parent, child, and associated resources.
    The owner identity is resolved by default. Pass include_owner=false to omit it.
    '''

    with Session(get_engine()) as session:
        return load_dataset_detail(
            session,
            auth_res,
            dataset_mfid,
            options=ResourceDetailOptions(
                include_metadata=params.include_metadata,
                include_links=params.include_links,
                include_owner=params.include_owner,
            ),
        )


@router.post("/datasets/{parent_dataset_mfid}/children/{child_dataset_mfid}", response_model=DatasetLinkRead)
def link_dataset_parent_child(parent_dataset_id: ParentDatasetMfid, child_dataset_id: ChildDatasetMfid, auth_res: Annotated[Principal, Depends(authorize_user)],
                              relationship_type: Optional[Literal["is_derived_from", "is_part_of"]] = Query(default=None)):
    '''
    Link two datasets as parent and child.

    relationship_type describes the child relative to the parent, so
    is_part_of means "child is_part_of parent". Omit it to leave the kind
    unspecified; on a link that already exists, omitting it preserves the
    type already stored rather than clearing it.
    Same-project links require contributor access to the parent and read access
    to the child. Cross-project links and relationship-type changes require
    editor access to both datasets.
    '''

    reject_self_link(parent_dataset_id, child_dataset_id)
    with Session(get_engine()) as session:
        parent = session.exec(select(Dataset).where(Dataset.unique_id == parent_dataset_id)).first()
        if not parent:
            raise HTTPException(status_code=404, detail="Parent dataset not found")
        child = session.exec(select(Dataset).where(Dataset.unique_id == child_dataset_id)).first()
        if not child:
            raise HTTPException(status_code=404, detail="Child dataset not found")

        require_link_create_permissions(session, auth_res, parent, child)

        link = session.exec(
            select(DatasetLink).where(
                DatasetLink.parent_dataset_id == parent.id,
                DatasetLink.child_dataset_id == child.id,
            )
        ).one_or_none()
        if link is not None:
            if relationship_type is None or link.relationship_type == relationship_type:
                return DatasetLinkRead(parent_dataset_mfid=parent_dataset_id, child_dataset_mfid=child_dataset_id, relationship_type=link.relationship_type)
            require_permission(session, auth_res, parent_dataset_id, Permission.EDIT)
            require_permission(session, auth_res, child_dataset_id, Permission.EDIT)
            link.relationship_type = relationship_type
        else:
            reject_lineage_cycle(session, DatasetLink.parent_dataset_id, DatasetLink.child_dataset_id, parent.id, child.id)
            link = DatasetLink(
                parent_dataset_id=parent.id,
                child_dataset_id=child.id,
                relationship_type=relationship_type,
            )
        session.add(link)

        touch_resource_modification_time(session, parent_dataset_id)
        touch_resource_modification_time(session, child_dataset_id)

        session.commit()
        return DatasetLinkRead(parent_dataset_mfid=parent_dataset_id, child_dataset_mfid=child_dataset_id, relationship_type=link.relationship_type)


@router.delete("/datasets/{parent_dataset_mfid}/children/{child_dataset_mfid}", response_model=MessageResponse)
def unlink_dataset_parent_child(parent_dataset_id: ParentDatasetMfid, child_dataset_id: ChildDatasetMfid, auth_res: Annotated[Principal, Depends(authorize_user)]):

    with Session(get_engine()) as session:
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


@router.get("/datasets/{child_dataset_mfid}/parents", response_model=PaginatedResponse[DatasetResponse])
def list_parent_datasets(child_dataset_id: ChildDatasetMfid, auth_res: Annotated[Principal, Depends(authorize_user)],
                         limit: int = Query(default=100, ge=1, le=1000),
                         offset: int = Query(default=0, ge=0),
                         relationship_type: Optional[Literal["is_derived_from", "is_part_of"]] = Query(default=None),
                         include_owner: bool = True):
    '''
    Returns a paginated list of parent datasets for a given dataset.

    relationship_type restricts the result to links of that kind. Links with no
    recorded type are excluded when the filter is set.
    '''
    with Session(get_engine()) as session:
        child = session.exec(select_active(Dataset, child_dataset_id)).one_or_none()
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
        if relationship_type is not None:
            query = query.where(DatasetLink.relationship_type == relationship_type)
        total = session.exec(select(func.count()).select_from(query.subquery())).one()
        parents = session.exec(query.offset(offset).limit(limit)).all()
        items = dataset_responses(session, auth_res, parents, include_owner)
        return PaginatedResponse(total=total, limit=limit, offset=offset, items=items)


@router.get("/datasets/{parent_dataset_mfid}/children", response_model=PaginatedResponse[DatasetResponse])
def list_child_datasets(parent_dataset_id: ParentDatasetMfid, auth_res: Annotated[Principal, Depends(authorize_user)],
                        limit: int = Query(default=100, ge=1, le=1000),
                        offset: int = Query(default=0, ge=0),
                        relationship_type: Optional[Literal["is_derived_from", "is_part_of"]] = Query(default=None),
                        include_owner: bool = True):
    '''
    Returns a paginated list of child datasets for a given parent dataset.

    relationship_type restricts the result to links of that kind. Links with no
    recorded type are excluded when the filter is set.
    '''
    with Session(get_engine()) as session:
        parent = session.exec(select_active(Dataset, parent_dataset_id)).one_or_none()
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
        if relationship_type is not None:
            query = query.where(DatasetLink.relationship_type == relationship_type)
        total = session.exec(select(func.count()).select_from(query.subquery())).one()
        children = session.exec(query.offset(offset).limit(limit)).all()
        items = dataset_responses(session, auth_res, children, include_owner)
        return PaginatedResponse(total=total, limit=limit, offset=offset, items=items)




@router.delete('/datasets/{dataset_mfid}/samples/{sample_mfid}', response_model=MessageResponse)
def unlink_dataset_sample(auth_res: Annotated[Principal, Depends(authorize_user)], dsid: DatasetMfid, sample_id: SampleMfid):

    with Session(get_engine()) as session:
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



def _thumbnail_read(thumbnail: Thumbnail, dataset_mfid: str) -> ThumbnailRead:
    return ThumbnailRead.model_validate(thumbnail, update={"dataset_mfid": dataset_mfid})


@router.get("/datasets/{dataset_mfid}/thumbnails", response_model=List[ThumbnailRead])
def read_thumbnails(auth_res: Annotated[Principal, Depends(authorize_user)], dsid: DatasetMfid):
    '''
    Returns a list of thumbnail objects associated with the dataset ID provided
    '''
    with Session(get_engine()) as session:
        ds_cmd = (select(Dataset)
                  .where(Dataset.unique_id == dsid)
                  .where(permission_filter(auth_res, Dataset, Permission.READ)))
        ds = session.exec(ds_cmd).one_or_none()

        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found or access denied")

        ds_thumbnails = session.exec(
            select(Thumbnail)
            .where(Thumbnail.dataset_id == ds.id)
            .order_by(Thumbnail.id)
        ).all()
        return [_thumbnail_read(tn, ds.unique_id) for tn in ds_thumbnails]


@router.post(
    "/datasets/first_thumbnails",
    response_model=dict[str, ThumbnailPreviewRead],
)
def get_first_thumbnails(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    dataset_ids: List[str] = Body(..., max_length=1000),
):
    '''
    Given a list of at most 1000 dataset unique_ids, returns a dict mapping each unique_id
    to its first (lowest-id) thumbnail. Datasets with no thumbnail or that the
    caller cannot access are omitted from the response.

    Intended for bulk thumbnail fetching after an entity_graph call.
    '''

    requested_count = len(dataset_ids)
    unique_dataset_ids = list(dict.fromkeys(dataset_ids))
    if requested_count > LARGE_THUMBNAIL_LOOKUP_WARNING_SIZE:
        logger.warning(
            "Large first-thumbnail lookup user_id=%s requested=%d unique=%d threshold=%d",
            auth_res.user_id,
            requested_count,
            len(unique_dataset_ids),
            LARGE_THUMBNAIL_LOOKUP_WARNING_SIZE,
        )

    with Session(get_engine()) as session:
        auth_filter = permission_filter(auth_res, Dataset, Permission.READ)

        authorized_ds = session.exec(
            select(Dataset.id, Dataset.unique_id)
            .filter(auth_filter)
            .where(Dataset.unique_id.in_(unique_dataset_ids))
        ).all()

        ds_id_to_uid = {
            dataset_id: unique_id
            for dataset_id, unique_id in authorized_ds
        }

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
                "id": t.id,
                "thumbnail_name": t.thumbnail_name,
                "thumbnail_b64str": t.thumbnail_b64str,
            }
            for t in first_thumbnails
        }
    
@router.post("/datasets/{dataset_mfid}/thumbnails", response_model=ThumbnailRead)
def add_thumbnail(auth_res: Annotated[Principal, Depends(authorize_user)],
                  dsid: DatasetMfid,
                  thumbnail: ThumbnailCreate):
    '''
    Adds a thumbnail to a dataset. Repeated uploads of the same normalized image
    to one dataset update that thumbnail's name.

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

    ds_cmd = (select(Dataset)
              .where(Dataset.unique_id == dsid)
              .where(permission_filter(auth_res, Dataset, Permission.EDIT)))

    with Session(get_engine()) as session:
        if session.exec(ds_cmd).one_or_none() is None:
            raise HTTPException(status_code=404, detail="Dataset not found")
        assert_not_pending_deletion(session, dsid)

    thumbnail_b64str = _normalize_thumbnail_input(dsid, thumbnail.thumbnail_b64str)

    with Session(get_engine()) as session:
        ds = session.exec(ds_cmd.with_for_update()).one_or_none()
        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found")

        tn_obj = add_or_update(
            session,
            Thumbnail,
            where=and_(
                Thumbnail.dataset_id == ds.id,
                Thumbnail.thumbnail_b64str == thumbnail_b64str,
            ),
            thumbnail_name=thumbnail.thumbnail_name,
            thumbnail_b64str=thumbnail_b64str,
            dataset_id=ds.id,
        )

        ri = session.exec(select(Resource).where(Resource.mfid == dsid)).one_or_none()
        if ri:
            ri.modification_time = get_utc_datetime()

        session.commit()
        session.refresh(tn_obj)
        return _thumbnail_read(tn_obj, ds.unique_id)


@router.patch(
    "/datasets/{dataset_mfid}/thumbnails/{thumbnail_id}",
    response_model=ThumbnailRead,
)
def update_thumbnail(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    dsid: DatasetMfid,
    thumbnail_id: int,
    update: ThumbnailUpdate,
):
    thumbnail_cmd = (
        select(Thumbnail)
        .join(Dataset, Thumbnail.dataset_id == Dataset.id)
        .where(
            Dataset.unique_id == dsid,
            Thumbnail.id == thumbnail_id,
            permission_filter(auth_res, Dataset, Permission.EDIT),
        )
    )

    with Session(get_engine()) as session:
        if session.exec(thumbnail_cmd).one_or_none() is None:
            raise HTTPException(status_code=404, detail="Thumbnail not found")

    normalized_b64str = None
    if update.thumbnail_b64str is not None:
        normalized_b64str = _normalize_thumbnail_input(dsid, update.thumbnail_b64str)

    with Session(get_engine()) as session:
        thumbnail = session.exec(thumbnail_cmd).one_or_none()
        if thumbnail is None:
            raise HTTPException(status_code=404, detail="Thumbnail not found")

        if update.thumbnail_name is not None:
            thumbnail.thumbnail_name = update.thumbnail_name
        if normalized_b64str is not None:
            thumbnail.thumbnail_b64str = normalized_b64str
        session.add(thumbnail)
        touch_resource_modification_time(session, dsid)
        session.commit()
        session.refresh(thumbnail)
        return _thumbnail_read(thumbnail, dsid)


@router.delete(
    "/datasets/{dataset_mfid}/thumbnails/{thumbnail_id}",
    response_model=dict[str, str],
)
def delete_thumbnail(auth_res: Annotated[Principal, Depends(authorize_user)], dsid: DatasetMfid, thumbnail_id: int):
    '''Removes a thumbnail from a dataset and deletes it.'''

    with Session(get_engine()) as session:
        ds = session.exec(
            select(Dataset)
            .where(Dataset.unique_id == dsid)
            .where(permission_filter(auth_res, Dataset, Permission.EDIT))
        ).one_or_none()
        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found")

        tn = session.exec(
            select(Thumbnail).where(Thumbnail.id == thumbnail_id, Thumbnail.dataset_id == ds.id)
        ).one_or_none()
        if tn is None:
            raise HTTPException(status_code=404, detail="Thumbnail not found")

        session.delete(tn)
        touch_resource_modification_time(session, dsid)
        session.commit()
        return {"message": f"Thumbnail {thumbnail_id} deleted from dataset {dsid}"}


@router.get("/datasets/{dataset_mfid}/download_links", response_model=Dict[str, Optional[str]], responses=STORAGE_NOT_CONFIGURED_RESPONSE)
def generate_dataset_download_links(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    dsid: DatasetMfid,
    disposition: Annotated[
        Literal["attachment", "inline"],
        Query(description="attachment (default) makes browsers save the file; inline asks them to display it when they can. Script-capable types (HTML, SVG, XML, +xml types, XSL) are always served as attachments."),
    ] = "attachment",
):
    '''
    Returns signed GCS download URLs (1-hour expiry) for all files in the dataset,
    keyed by AssociatedFile mfid. Backfills missing storage_path records on the fly.
    '''

    with Session(get_engine()) as session:
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
                mfid_to_path[af.mfid] = (_ensure_storage_path(af, dsid, session), af.storage_generation, af.filename)
            except HTTPException:
                mfid_to_path[af.mfid] = None
        session.commit()

    # Generate signed URLs outside the DB session; null for missing blobs. Keyless signing is a call
    # to Google per URL (about 0.1 s), so a large dataset is signed in parallel.
    def sign(item):
        mfid, storage_path = item
        if storage_path is None:
            return mfid, None
        try:
            path, generation, filename = storage_path
            return mfid, _signed_download(mfid, dsid, path, generation, filename, disposition)[0]
        except HTTPException:
            return mfid, None

    with ThreadPoolExecutor(max_workers=DOWNLOAD_SIGNING_WORKERS) as pool:
        return dict(pool.map(sign, mfid_to_path.items()))


@router.get("/datasets/{dataset_mfid}/files", response_model=List[AssociatedFileRead])
def read_associated_files(auth_res: Annotated[Principal, Depends(authorize_user)], dsid: DatasetMfid):
    '''
    Returns a list of the files associated with the dataset ID provided.
    '''

    with Session(get_engine()) as session:
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


@router.post("/datasets/{dataset_mfid}/files", response_model=AssociatedFileRead, responses={409: {"description": "A file with this filename already exists in the dataset with different content: `{\"detail\": {\"code\": \"filename_exists\", \"file_mfid\": ..., \"etag\": ..., \"message\": ...}}`; update it through `PATCH /files/{file_mfid}` (remote files) or `POST /files/{file_mfid}/upload/multipart/initiate` (GCS files). A filename reserved by an active multipart upload returns a plain string detail"}})
def add_associated_file(auth_res: Annotated[Principal, Depends(authorize_user)],
                        dsid: DatasetMfid,
                        associated_file: AssociatedFileCreate):
    '''
    Adds a file record to a dataset.
    Filenames are unique within a dataset, so identical content under different filenames gets separate records.
    Repeating a filename with the same sha256_hash, size, and storage_backend returns the existing record unchanged
    (storage_path and access_note are not compared); any other difference returns 409 `filename_exists` with the
    existing file's MFID and ETag. Non-GCS filenames must be canonical relative POSIX paths (422 otherwise).
    Use POST /files/{file_id}/ingest to trigger ingestion.
    Requires contributor access.
    '''

    with Session(get_engine()) as session:
        ds = session.exec(select(Dataset).where(Dataset.unique_id == dsid)).one_or_none()
        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found")
        require_permission(session, auth_res, dsid, Permission.CREATE)
        assert_not_pending_deletion(session, dsid)

        dataset_pk = ds.id

        def same_name():
            return session.exec(
                select(AssociatedFile).where(
                    AssociatedFile.dataset_id == dataset_pk,
                    AssociatedFile.filename == associated_file.filename,
                )
            ).first()

        def retry_or_conflict(existing):
            if all(getattr(existing, field) == getattr(associated_file, field) for field in ("sha256_hash", "size", "storage_backend")):
                return AssociatedFileRead.model_validate(existing, update={"dataset_mfid": dsid})
            raise HTTPException(409, {"code": "filename_exists", "file_mfid": existing.mfid, "etag": file_etag(existing.storage_path, existing.storage_generation, existing.sha256_hash, existing.size), "message": "Filename already exists"})

        existing = same_name()
        if existing is not None:
            return retry_or_conflict(existing)

        if multipart.filename_reserved(session, dsid, associated_file.filename):
            raise HTTPException(status_code=409, detail="Filename is reserved by an active multipart upload")

        af_obj = AssociatedFile(
            **associated_file.model_dump(),
            dataset_id=dataset_pk,
        )
        session.add(af_obj)
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            existing = same_name()
            if existing is None:
                raise
            return retry_or_conflict(existing)

        ri = session.exec(select(Resource).where(Resource.mfid == dsid)).one_or_none()
        if ri:
            ri.modification_time = get_utc_datetime()

        session.commit()
        session.refresh(af_obj)
        return AssociatedFileRead.model_validate(af_obj, update={"dataset_mfid": dsid})


@router.post("/datasets/{dataset_mfid}/files/{file_id}/ingest", deprecated=True, status_code=410, responses=REMOVED_RESPONSE)
def create_ds_file_ingestion_request(dataset_mfid: str, file_id: str):
    """Removed: use POST /files/{file_id}/ingest."""
    removed("This route was removed. Request ingestion of a file with POST /files/{file_id}/ingest.")


@router.get("/datasets/{dataset_mfid}/keywords", response_model=List[KeywordRead])
def list_keywords_for_dataset(auth_res: Annotated[Principal, Depends(authorize_user)], dsid: DatasetMfid):


    with Session(get_engine()) as session:
        ds_cmd = (select(Dataset)
                  .where(Dataset.unique_id == dsid)
                  .where(permission_filter(auth_res, Dataset, Permission.READ)))
        ds = session.exec(ds_cmd).one_or_none()

        if ds is None:
            raise HTTPException(status_code=403, detail="Dataset not found or access denied")

        ds_keywords = session.exec(select(Keyword).select_from(join(Keyword, DatasetKeywordLink, DatasetKeywordLink.keyword_id == Keyword.id)).where(DatasetKeywordLink.dataset_id == ds.id)).all()
        return ds_keywords


@router.post("/datasets/{dataset_mfid}/keywords", response_model=KeywordRead)
def add_keyword(auth_res: Annotated[Principal, Depends(authorize_user)], dsid: DatasetMfid, keyword: str):
    '''
    Adds a keyword to a dataset. If the keyword exists, it will simply add the existing keyword to the dataset object.
    Otherwise, the keyword will be created and then added to the dataset.
    Requires contributor access.
    '''
    ds_cmd = (select(Dataset)
              .where(Dataset.unique_id == dsid)
              .where(permission_filter(auth_res, Dataset, Permission.CREATE)))

    with Session(get_engine()) as session:
        kw_obj = add_or_update(session, Keyword, where=(Keyword.keyword == keyword), keyword=keyword)

        ds = session.exec(ds_cmd).one_or_none()

        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found")

        added = kw_obj not in ds.keywords
        if added:
            ds.keywords.append(kw_obj)

        count = session.exec(select(func.count(DatasetKeywordLink.dataset_id))\
                            .where(DatasetKeywordLink.keyword_id == kw_obj.id)).one()

        kw_obj.num_datasets = count
        session.add(kw_obj)
        session.add(ds)
        if added:
            touch_resource_modification_time(session, dsid)
        session.commit()
        session.refresh(kw_obj)
        return(kw_obj)


def _dataset_sample_link_response(ds: Dataset, samp: Sample) -> DatasetSampleLinkResponse:
    return DatasetSampleLinkResponse(
        dataset_id=ds.unique_id,
        dataset_mfid=ds.unique_id,
        dataset_name=ds.dataset_name,
        sample_id=samp.unique_id,
        sample_mfid=samp.unique_id,
        sample_type=samp.sample_type,
        sample_name=samp.sample_name,
    )


@router.post("/datasets/{dataset_mfid}/samples/{sample_mfid}", response_model=DatasetSampleLinkResponse)
def link_dataset_sample(auth_res: Annotated[Principal, Depends(authorize_user)], dsid: DatasetMfid, sample_id: SampleMfid):
    '''Links a dataset to a readable sample in the same project with contributor access.

    Cross-project links retain the editor requirement on both resources.
    '''

    with Session(get_engine()) as session:
        ds = session.exec(select(Dataset).where(Dataset.unique_id == dsid)).one_or_none()
        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found")
        samp = session.exec(select(Sample).where(Sample.unique_id == sample_id)).one_or_none()
        if samp is None:
            raise HTTPException(status_code=404, detail="Sample not found")

        require_link_create_permissions(session, auth_res, ds, samp)

        link = session.exec(
            select(DatasetSampleLink).where(
                DatasetSampleLink.sample_id == samp.id,
                DatasetSampleLink.dataset_id == ds.id,
            )
        ).one_or_none()
        if link is not None:
            return _dataset_sample_link_response(ds, samp)

        session.add(DatasetSampleLink(sample_id=samp.id, dataset_id=ds.id))

        touch_resource_modification_time(session, dsid)
        touch_resource_modification_time(session, sample_id)
        session.commit()
        return _dataset_sample_link_response(ds, samp)


@router.get("/datasets/{dataset_mfid}/samples", deprecated=True, status_code=410, responses=REMOVED_RESPONSE)
def read_samples_in_dataset(dataset_mfid: str):
    """Removed: use GET /samples?dataset_mfid={dataset_mfid}."""
    removed("This route was removed. List a dataset's samples with GET /samples?dataset_mfid={dataset_mfid}.")


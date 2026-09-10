from fastapi import HTTPException
from sqlalchemy import exists, func, or_
from sqlmodel import Session, select

from app.models.core_models import Dataset, DatasetSampleLink, Resource, Sample, User
from app.models.facets import (
    FacetBucket,
    FacetResponse,
    InstrumentFacetBucket,
    OwnerFacetBucket,
    ProjectFacetBucket,
)
from app.models.query_params import (
    DatasetFacetField,
    DatasetFacetQuery,
    ResourceFacetQuery,
    SampleFacetField,
    SampleFacetQuery,
)
from app.models.users import UserPublicRead
from app.services.resource_filters import resource_scope_predicates
from app.utils.auth_utils import permission_filter
from app.utils.cursor_utils import CursorError, filter_fingerprint, new_cursor, validate_cursor
from app.utils.instrument_utils import resolve_instrument_mfids_to_references
from app.utils.permissions import Permission, Principal
from app.utils.project_utils import resolve_project_filter_mfid, resolve_project_mfids_to_references


DATASET_FACET_COLUMNS = {
    DatasetFacetField.SESSION: Dataset.session_name,
    DatasetFacetField.MEASUREMENT: Dataset.measurement,
    DatasetFacetField.DATA_FORMAT: Dataset.data_format,
    DatasetFacetField.OWNER: Dataset.owner_orcid,
    DatasetFacetField.INSTRUMENT: Dataset.instrument_mfid,
    DatasetFacetField.PROJECT: Dataset.project_mfid,
}

SAMPLE_FACET_COLUMNS = {
    SampleFacetField.SAMPLE_TYPE: Sample.sample_type,
    SampleFacetField.OWNER: Sample.owner_orcid,
    SampleFacetField.PROJECT: Sample.project_mfid,
}


def _apply_null_filter(query, column, value: bool | None):
    if value is None:
        return query
    return query.where(column.is_(None) if value else column.is_not(None))


def _facet_page(
    session: Session,
    principal: Principal,
    params: ResourceFacetQuery,
    query,
    column,
    kind: str,
):
    filters = filter_fingerprint(params.cursor_filters())
    if params.cursor is not None:
        try:
            cursor = validate_cursor(
                params.cursor,
                kind=kind,
                sort=params.field.value,
                direction="asc",
                filters=filters,
                user_id=principal.user_id,
            )
        except CursorError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if cursor is None or cursor.primary is None:
            raise HTTPException(status_code=422, detail="Invalid facet cursor")
        query = query.having(or_(column > cursor.primary, column.is_(None)))

    query = query.group_by(column).order_by(column.asc().nulls_last()).limit(params.limit + 1)
    rows = session.exec(query).all()
    has_more = len(rows) > params.limit
    rows = rows[:params.limit]
    next_cursor = None
    if has_more:
        last_value = rows[-1][0]
        if last_value is None:
            raise RuntimeError("Null facet bucket must be last")
        next_cursor = new_cursor(
            kind=kind,
            sort=params.field.value,
            direction="asc",
            primary=last_value,
            unique_id="bucket",
            filters=filters,
            user_id=principal.user_id,
        )
    return rows, next_cursor


def _owner_buckets(session: Session, rows) -> list[OwnerFacetBucket]:
    values = [value for value, _ in rows if value is not None]
    owners = {
        owner.unique_id: UserPublicRead.model_validate(owner)
        for owner in session.exec(select(User).where(User.unique_id.in_(values))).all()
    } if values else {}
    return [
        OwnerFacetBucket(value=value, count=count, owner=owners.get(value))
        for value, count in rows
    ]


def _project_buckets(session: Session, rows) -> list[ProjectFacetBucket]:
    references = resolve_project_mfids_to_references([value for value, _ in rows], session)
    return [
        ProjectFacetBucket(value=value, count=count, project=references.get(value))
        for value, count in rows
    ]


def _instrument_buckets(session: Session, rows) -> list[InstrumentFacetBucket]:
    references = resolve_instrument_mfids_to_references([value for value, _ in rows], session)
    return [
        InstrumentFacetBucket(value=value, count=count, instrument=references.get(value))
        for value, count in rows
    ]


def list_dataset_facets(
    session: Session,
    principal: Principal,
    params: DatasetFacetQuery,
) -> FacetResponse:
    column = DATASET_FACET_COLUMNS[params.field]
    query = (
        select(column, func.count())
        .select_from(Dataset)
        .join(Resource, Resource.mfid == Dataset.unique_id, isouter=True)
    )
    project_mfid = resolve_project_filter_mfid(session, params.project_id, params.project_mfid)
    query = query.where(*resource_scope_predicates(session, principal, Dataset, params, project_mfid))

    if params.sample_mfid is not None:
        sample_id = session.exec(
            select(Sample.id)
            .where(Sample.unique_id == params.sample_mfid)
            .where(permission_filter(principal, Sample, Permission.READ))
        ).one_or_none()
        if sample_id is None:
            raise HTTPException(status_code=404, detail="Sample not found or access denied")
        query = query.where(
            exists()
            .where(
                DatasetSampleLink.sample_id == sample_id,
                DatasetSampleLink.dataset_id == Dataset.id,
            )
            .correlate(Dataset)
        )
    for filter_column, value in (
        (Dataset.instrument_mfid, params.instrument_mfid),
        (Dataset.session_name, params.session_name),
        (Dataset.measurement, params.measurement),
        (Dataset.data_format, params.data_format),
        (Dataset.owner_orcid, params.owner_id),
    ):
        if value is not None:
            query = query.where(filter_column == value)
    for filter_column, value in (
        (Dataset.session_name, params.session_name_is_null),
        (Dataset.measurement, params.measurement_is_null),
        (Dataset.data_format, params.data_format_is_null),
        (Dataset.owner_orcid, params.owner_id_is_null),
        (Dataset.instrument_mfid, params.instrument_mfid_is_null),
        (Dataset.project_mfid, params.project_mfid_is_null),
    ):
        query = _apply_null_filter(query, filter_column, value)

    rows, next_cursor = _facet_page(
        session,
        principal,
        params,
        query,
        column,
        "dataset_facet",
    )
    if params.field is DatasetFacetField.OWNER:
        items = _owner_buckets(session, rows)
    elif params.field is DatasetFacetField.PROJECT:
        items = _project_buckets(session, rows)
    elif params.field is DatasetFacetField.INSTRUMENT:
        items = _instrument_buckets(session, rows)
    else:
        items = [FacetBucket(value=value, count=count) for value, count in rows]
    return FacetResponse(field=params.field.value, limit=params.limit, items=items, next_cursor=next_cursor)


def list_sample_facets(
    session: Session,
    principal: Principal,
    params: SampleFacetQuery,
) -> FacetResponse:
    column = SAMPLE_FACET_COLUMNS[params.field]
    query = (
        select(column, func.count())
        .select_from(Sample)
        .join(Resource, Resource.mfid == Sample.unique_id, isouter=True)
    )
    project_mfid = resolve_project_filter_mfid(session, params.project_id, params.project_mfid)
    query = query.where(*resource_scope_predicates(session, principal, Sample, params, project_mfid))

    if params.dataset_mfid is not None:
        dataset_id = session.exec(
            select(Dataset.id)
            .where(Dataset.unique_id == params.dataset_mfid)
            .where(permission_filter(principal, Dataset, Permission.READ))
        ).one_or_none()
        if dataset_id is None:
            raise HTTPException(status_code=404, detail="Dataset not found or access denied")
        query = query.where(
            exists()
            .where(
                DatasetSampleLink.dataset_id == dataset_id,
                DatasetSampleLink.sample_id == Sample.id,
            )
            .correlate(Sample)
        )
    for filter_column, value in (
        (Sample.sample_type, params.sample_type),
        (Sample.owner_orcid, params.owner_id),
    ):
        if value is not None:
            query = query.where(filter_column == value)
    for filter_column, value in (
        (Sample.sample_type, params.sample_type_is_null),
        (Sample.owner_orcid, params.owner_id_is_null),
        (Sample.project_mfid, params.project_mfid_is_null),
    ):
        query = _apply_null_filter(query, filter_column, value)

    rows, next_cursor = _facet_page(
        session,
        principal,
        params,
        query,
        column,
        "sample_facet",
    )
    if params.field is SampleFacetField.OWNER:
        items = _owner_buckets(session, rows)
    elif params.field is SampleFacetField.PROJECT:
        items = _project_buckets(session, rows)
    else:
        items = [FacetBucket(value=value, count=count) for value, count in rows]
    return FacetResponse(field=params.field.value, limit=params.limit, items=items, next_cursor=next_cursor)

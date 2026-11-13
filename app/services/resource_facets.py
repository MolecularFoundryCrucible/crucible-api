from fastapi import HTTPException
from sqlalchemy import and_, case, exists, func, literal, or_
from sqlmodel import Session, select

from app.models.query_params import requested_owners
from app.utils.user_utils import owned_by_any
from app.tables import (
    Dataset,
    DatasetSampleLink,
    Instrument,
    Project,
    Resource,
    Sample,
    User,
)
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
    FacetSort,
    ResourceFacetQuery,
    SampleFacetField,
    SampleFacetQuery,
    SortDirection,
)
from app.models.users import UserPublicRead
from app.services.resource_filters import resource_scope_predicates
from app.utils.cursor_utils import CursorError, filter_fingerprint, new_cursor, validate_cursor
from app.utils.instrument_utils import resolve_instrument_filter_mfid, resolve_instrument_mfids_to_references
from app.utils.permissions import Principal
from app.utils.project_utils import resolve_project_filter_mfid, resolve_project_mfids_to_references
from app.utils.sql_utils import select_active


DATASET_FACET_COLUMNS = {
    DatasetFacetField.SESSION: Dataset.session_name,
    DatasetFacetField.MEASUREMENT: Dataset.measurement,
    DatasetFacetField.DATA_FORMAT: Dataset.data_format,
    DatasetFacetField.OWNER: User.unique_id,
    DatasetFacetField.INSTRUMENT: Dataset.instrument_mfid,
    DatasetFacetField.PROJECT: Dataset.project_mfid,
}

SAMPLE_FACET_COLUMNS = {
    SampleFacetField.SAMPLE_TYPE: Sample.sample_type,
    SampleFacetField.OWNER: User.unique_id,
    SampleFacetField.PROJECT: Sample.project_mfid,
}

COUNT_CURSOR_VALUE_PREFIX = "value:"


def _apply_null_filter(query, column, value: bool | None):
    if value is None:
        return query
    return query.where(column.is_(None) if value else column.is_not(None))


def _nonblank_sql(column):
    return func.nullif(func.trim(column), "")


def _facet_label_expression(field, column, owner_user_id_column):
    if field.value == "owner":
        first_name = func.coalesce(_nonblank_sql(User.first_name), "")
        last_name = func.coalesce(_nonblank_sql(User.last_name), "")
        full_name = _nonblank_sql(first_name + literal(" ") + last_name)
        return (
            func.coalesce(full_name, _nonblank_sql(User.username), column),
            (User, User.id == owner_user_id_column),
        )
    if field.value == "project":
        return (
            func.coalesce(
                _nonblank_sql(Project.title),
                _nonblank_sql(Project.project_id),
                column,
            ),
            (Project, Project.unique_id == column),
        )
    if field.value == "instrument":
        return (
            func.coalesce(
                _nonblank_sql(Instrument.instrument_name),
                _nonblank_sql(Instrument.instrument_id),
                column,
            ),
            (Instrument, Instrument.unique_id == column),
        )
    return column, None


def _facet_label_for_value(session: Session, field, value: str) -> str:
    if field.value == "owner":
        user = session.exec(select(User).where(User.unique_id == value)).one_or_none()
        if user is None:
            return value
        full_name = " ".join(
            part.strip()
            for part in (user.first_name, user.last_name)
            if part and part.strip()
        )
        return full_name or (user.username or "").strip() or value
    if field.value == "project":
        project = session.exec(select(Project).where(Project.unique_id == value)).one_or_none()
        if project is None:
            return value
        return (project.title or "").strip() or project.project_id.strip() or value
    if field.value == "instrument":
        instrument = session.exec(
            select(Instrument).where(Instrument.unique_id == value)
        ).one_or_none()
        if instrument is None:
            return value
        return (
            (instrument.instrument_name or "").strip()
            or (instrument.instrument_id or "").strip()
            or value
        )
    return value


def _facet_page(
    session: Session,
    principal: Principal,
    params: ResourceFacetQuery,
    query,
    column,
    label_expression,
    kind: str,
):
    filters = filter_fingerprint(params.cursor_filters())
    cursor_sort = params.field.value
    if params.sort is not FacetSort.VALUE:
        cursor_sort = f"{params.field.value}:{params.sort.value}"
    bucket_count = func.count()
    label_key = func.lower(label_expression)
    if params.cursor is not None:
        try:
            cursor = validate_cursor(
                params.cursor,
                kind=kind,
                sort=cursor_sort,
                direction=params.direction.value,
                filters=filters,
                user_id=principal.user_id,
            )
        except CursorError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if cursor is None or cursor.primary is None:
            raise HTTPException(status_code=422, detail="Invalid facet cursor")
        if params.sort is FacetSort.COUNT:
            try:
                cursor_count = int(cursor.primary)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail="Invalid facet cursor") from exc
            if cursor_count < 1 or not cursor.unique_id.startswith(COUNT_CURSOR_VALUE_PREFIX):
                raise HTTPException(status_code=422, detail="Invalid facet cursor")
            cursor_value = cursor.unique_id[len(COUNT_CURSOR_VALUE_PREFIX):]
            count_after = (
                bucket_count > cursor_count
                if params.direction is SortDirection.ASC
                else bucket_count < cursor_count
            )
            query = query.having(or_(
                column.is_(None),
                count_after,
                and_(bucket_count == cursor_count, column > cursor_value),
            ))
        elif params.sort is FacetSort.LABEL:
            cursor_value = cursor.primary
            cursor_label_key = func.lower(
                _facet_label_for_value(session, params.field, cursor_value)
            )
            label_after = (
                label_key > cursor_label_key
                if params.direction is SortDirection.ASC
                else label_key < cursor_label_key
            )
            query = query.having(or_(
                column.is_(None),
                label_after,
                and_(label_key == cursor_label_key, column > cursor_value),
            ))
        else:
            value_after = (
                column > cursor.primary
                if params.direction is SortDirection.ASC
                else column < cursor.primary
            )
            query = query.having(or_(value_after, column.is_(None)))

    query = query.group_by(column, label_expression)
    if params.sort is FacetSort.COUNT:
        count_order = (
            bucket_count.asc()
            if params.direction is SortDirection.ASC
            else bucket_count.desc()
        )
        query = query.order_by(
            case((column.is_(None), 1), else_=0).asc(),
            count_order,
            column.asc(),
        )
    elif params.sort is FacetSort.LABEL:
        label_order = (
            label_key.asc()
            if params.direction is SortDirection.ASC
            else label_key.desc()
        )
        query = query.order_by(
            case((column.is_(None), 1), else_=0).asc(),
            label_order,
            column.asc(),
        )
    else:
        value_order = (
            column.asc().nulls_last()
            if params.direction is SortDirection.ASC
            else column.desc().nulls_last()
        )
        query = query.order_by(value_order)
    query = query.limit(params.limit + 1)
    rows = session.exec(query).all()
    has_more = len(rows) > params.limit
    rows = rows[:params.limit]
    next_cursor = None
    if has_more:
        last_value, last_count, _ = rows[-1]
        if last_value is None:
            raise RuntimeError("Null facet bucket must be last")
        next_cursor = new_cursor(
            kind=kind,
            sort=cursor_sort,
            direction=params.direction.value,
            primary=last_count if params.sort is FacetSort.COUNT else last_value,
            unique_id=(
                f"{COUNT_CURSOR_VALUE_PREFIX}{last_value}"
                if params.sort is FacetSort.COUNT
                else "bucket"
            ),
            filters=filters,
            user_id=principal.user_id,
        )
    return rows, next_cursor


def _owner_buckets(session: Session, rows) -> list[OwnerFacetBucket]:
    values = [value for value, _, _ in rows if value is not None]
    owners = {
        owner.unique_id: UserPublicRead.model_validate(owner)
        for owner in session.exec(select(User).where(User.unique_id.in_(values))).all()
    } if values else {}
    return [
        OwnerFacetBucket(value=value, label=label, count=count, owner=owners.get(value))
        for value, count, label in rows
    ]


def _project_buckets(session: Session, rows) -> list[ProjectFacetBucket]:
    references = resolve_project_mfids_to_references(
        [value for value, _, _ in rows],
        session,
    )
    return [
        ProjectFacetBucket(value=value, label=label, count=count, project=references.get(value))
        for value, count, label in rows
    ]


def _instrument_buckets(session: Session, rows) -> list[InstrumentFacetBucket]:
    references = resolve_instrument_mfids_to_references(
        [value for value, _, _ in rows],
        session,
    )
    return [
        InstrumentFacetBucket(
            value=value,
            label=label,
            count=count,
            instrument=references.get(value),
        )
        for value, count, label in rows
    ]


def list_dataset_facets(
    session: Session,
    principal: Principal,
    params: DatasetFacetQuery,
) -> FacetResponse:
    column = DATASET_FACET_COLUMNS[params.field]
    label_expression, label_join = _facet_label_expression(params.field, column, Dataset.owner_user_id)
    query = (
        select(column, func.count(), label_expression.label("label"))
        .select_from(Dataset)
        .join(Resource, Resource.mfid == Dataset.unique_id, isouter=True)
    )
    if label_join is not None:
        query = query.join(*label_join, isouter=True)
    project_mfid = resolve_project_filter_mfid(session, params.project_id, params.project_mfid)
    query = query.where(*resource_scope_predicates(session, principal, Dataset, params, project_mfid))

    if params.sample_mfid is not None:
        sample_id = session.exec(
            select_active(Sample, params.sample_mfid, Sample.id, principal=principal)
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
    instrument_mfid = resolve_instrument_filter_mfid(session, params.instrument_id, params.instrument_mfid)
    for filter_column, value in (
        (Dataset.instrument_mfid, instrument_mfid),
        (Dataset.session_name, params.session_name),
        (Dataset.measurement, params.measurement),
        (Dataset.data_format, params.data_format),
    ):
        if value is not None:
            query = query.where(filter_column == value)
    if owners := requested_owners(params):
        query = query.where(owned_by_any(session, Dataset.owner_user_id, owners))
    for filter_column, value in (
        (Dataset.session_name, params.session_name_is_null),
        (Dataset.measurement, params.measurement_is_null),
        (Dataset.data_format, params.data_format_is_null),
        (Dataset.owner_user_id, params.owner_id_is_null),
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
        label_expression,
        "dataset_facet",
    )
    if params.field is DatasetFacetField.OWNER:
        items = _owner_buckets(session, rows)
    elif params.field is DatasetFacetField.PROJECT:
        items = _project_buckets(session, rows)
    elif params.field is DatasetFacetField.INSTRUMENT:
        items = _instrument_buckets(session, rows)
    else:
        items = [
            FacetBucket(value=value, label=label, count=count)
            for value, count, label in rows
        ]
    return FacetResponse(field=params.field.value, limit=params.limit, items=items, next_cursor=next_cursor)


def list_sample_facets(
    session: Session,
    principal: Principal,
    params: SampleFacetQuery,
) -> FacetResponse:
    column = SAMPLE_FACET_COLUMNS[params.field]
    label_expression, label_join = _facet_label_expression(params.field, column, Sample.owner_user_id)
    query = (
        select(column, func.count(), label_expression.label("label"))
        .select_from(Sample)
        .join(Resource, Resource.mfid == Sample.unique_id, isouter=True)
    )
    if label_join is not None:
        query = query.join(*label_join, isouter=True)
    project_mfid = resolve_project_filter_mfid(session, params.project_id, params.project_mfid)
    query = query.where(*resource_scope_predicates(session, principal, Sample, params, project_mfid))

    if params.dataset_mfid is not None:
        dataset_id = session.exec(
            select_active(Dataset, params.dataset_mfid, Dataset.id, principal=principal)
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
    ):
        if value is not None:
            query = query.where(filter_column == value)
    if owners := requested_owners(params):
        query = query.where(owned_by_any(session, Sample.owner_user_id, owners))
    for filter_column, value in (
        (Sample.sample_type, params.sample_type_is_null),
        (Sample.owner_user_id, params.owner_id_is_null),
        (Sample.project_mfid, params.project_mfid_is_null),
    ):
        query = _apply_null_filter(query, filter_column, value)

    rows, next_cursor = _facet_page(
        session,
        principal,
        params,
        query,
        column,
        label_expression,
        "sample_facet",
    )
    if params.field is SampleFacetField.OWNER:
        items = _owner_buckets(session, rows)
    elif params.field is SampleFacetField.PROJECT:
        items = _project_buckets(session, rows)
    else:
        items = [
            FacetBucket(value=value, label=label, count=count)
            for value, count, label in rows
        ]
    return FacetResponse(field=params.field.value, limit=params.limit, items=items, next_cursor=next_cursor)

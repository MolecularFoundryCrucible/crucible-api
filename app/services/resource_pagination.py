from dataclasses import dataclass
from typing import Any, Mapping

from fastapi import HTTPException
from sqlmodel import Session, and_, or_

from app.models.query_params import ResourceCollectionQuery, ResourceSort
from app.utils.cursor_utils import (
    CursorError,
    ResourceCursor,
    cursor_primary_value,
    filter_fingerprint,
    new_cursor,
    validate_cursor,
)


@dataclass(frozen=True)
class ResourcePaginationSpec:
    kind: str
    unique_id_column: Any
    created_sort: Any
    updated_sort: Any
    name_sort: Any

    def primary_sort(self, sort: ResourceSort | None):
        if sort is ResourceSort.CREATED:
            return self.created_sort
        if sort is ResourceSort.UPDATED:
            return self.updated_sort
        if sort is ResourceSort.NAME:
            return self.name_sort
        return None


@dataclass(frozen=True)
class ResourcePagination:
    spec: ResourcePaginationSpec
    sort: ResourceSort | None
    sort_name: str
    direction: str
    limit: int
    cursor: str | None
    anchor_mfid: str | None
    offset: int
    filters_digest: str
    user_id: str
    decoded_cursor: ResourceCursor | None
    decoded_primary: Any

    @property
    def primary_sort(self):
        return self.spec.primary_sort(self.sort)

    def should_include_total(self, include_total: bool | None) -> bool:
        if include_total is not None:
            return include_total
        return self.cursor is None and self.offset == 0

    @property
    def response_offset(self) -> int | None:
        if self.anchor_mfid is None and self.cursor is None and self.offset:
            return self.offset
        return None


@dataclass(frozen=True)
class ResourcePage:
    rows: list[Any]
    next_cursor: str | None


def prepare_resource_pagination(
    *,
    params: ResourceCollectionQuery,
    query_params: Mapping[str, str],
    spec: ResourcePaginationSpec,
    result_filters: dict[str, Any],
    user_id: str,
) -> ResourcePagination:
    if params.sort is None and "direction" in query_params:
        raise HTTPException(status_code=422, detail="direction requires sort")
    if params.anchor_mfid is not None and (
        params.cursor is not None or "offset" in query_params
    ):
        raise HTTPException(
            status_code=422,
            detail="anchor_mfid cannot be combined with cursor or offset",
        )

    sort_name = params.sort.value if params.sort is not None else "unique_id"
    direction = params.direction.value if params.sort is not None else "desc"
    filters_digest = filter_fingerprint(result_filters)
    decoded_cursor = None
    decoded_primary = None
    if params.cursor is not None:
        try:
            decoded_cursor = validate_cursor(
                params.cursor,
                kind=spec.kind,
                sort=sort_name,
                direction=direction,
                filters=filters_digest,
                user_id=user_id,
            )
            if decoded_cursor is not None:
                decoded_primary = cursor_primary_value(decoded_cursor)
        except CursorError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return ResourcePagination(
        spec=spec,
        sort=params.sort,
        sort_name=sort_name,
        direction=direction,
        limit=params.limit,
        cursor=params.cursor,
        anchor_mfid=params.anchor_mfid,
        offset=params.offset,
        filters_digest=filters_digest,
        user_id=user_id,
        decoded_cursor=decoded_cursor,
        decoded_primary=decoded_primary,
    )


def _keyset_predicate(
    *,
    primary_sort,
    primary_value: Any,
    unique_id_column,
    unique_id: str,
    direction: str,
    inclusive: bool,
):
    if direction == "asc":
        id_comparison = (
            unique_id_column >= unique_id
            if inclusive
            else unique_id_column > unique_id
        )
    else:
        id_comparison = (
            unique_id_column <= unique_id
            if inclusive
            else unique_id_column < unique_id
        )
    if primary_sort is None:
        return id_comparison
    if primary_value is None:
        return and_(primary_sort.is_(None), id_comparison)
    primary_comparison = (
        primary_sort > primary_value
        if direction == "asc"
        else primary_sort < primary_value
    )
    return or_(
        primary_comparison,
        and_(primary_sort == primary_value, id_comparison),
        primary_sort.is_(None),
    )


def execute_resource_page(
    session: Session,
    query,
    pagination: ResourcePagination,
) -> ResourcePage:
    primary_sort = pagination.primary_sort
    unique_id_column = pagination.spec.unique_id_column

    if pagination.anchor_mfid is not None:
        anchor_query = query.where(unique_id_column == pagination.anchor_mfid)
        if primary_sort is not None:
            anchor_query = anchor_query.add_columns(primary_sort)
        anchor_row = session.exec(anchor_query).first()
        if anchor_row is None:
            raise HTTPException(status_code=404, detail="Anchor not found in the filtered collection")
        anchor_primary = anchor_row[-1] if primary_sort is not None else None
        query = query.where(
            _keyset_predicate(
                primary_sort=primary_sort,
                primary_value=anchor_primary,
                unique_id_column=unique_id_column,
                unique_id=pagination.anchor_mfid,
                direction=pagination.direction,
                inclusive=True,
            )
        )
    elif pagination.cursor is not None:
        if pagination.decoded_cursor is None:
            query = query.where(unique_id_column < pagination.cursor)
        else:
            query = query.where(
                _keyset_predicate(
                    primary_sort=primary_sort,
                    primary_value=pagination.decoded_primary,
                    unique_id_column=unique_id_column,
                    unique_id=pagination.decoded_cursor.unique_id,
                    direction=pagination.direction,
                    inclusive=False,
                )
            )

    if primary_sort is None:
        query = query.order_by(unique_id_column.desc())
    elif pagination.direction == "asc":
        query = query.order_by(primary_sort.asc().nulls_last(), unique_id_column.asc())
    else:
        query = query.order_by(
            primary_sort.desc().nulls_last(),
            unique_id_column.desc(),
        )
    if pagination.anchor_mfid is None and pagination.cursor is None and pagination.offset:
        query = query.offset(pagination.offset)

    includes_primary = primary_sort is not None
    if includes_primary:
        query = query.add_columns(primary_sort)
    raw_rows = session.exec(query.limit(pagination.limit + 1)).all()
    has_more = len(raw_rows) > pagination.limit
    raw_rows = raw_rows[:pagination.limit]
    rows = [row[:-1] for row in raw_rows] if includes_primary else raw_rows

    next_cursor = None
    if has_more:
        last_row = raw_rows[-1]
        next_cursor = new_cursor(
            kind=pagination.spec.kind,
            sort=pagination.sort_name,
            direction=pagination.direction,
            primary=last_row[-1] if includes_primary else None,
            unique_id=last_row[0].unique_id,
            filters=pagination.filters_digest,
            user_id=pagination.user_id,
        )
    return ResourcePage(rows=rows, next_cursor=next_cursor)

import json
import logging
from typing import Annotated, Optional, Union

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query

logger = logging.getLogger(__name__)
from sqlalchemy import and_, case, false, func, or_
from sqlalchemy.orm import aliased
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.api.path_params import ResourceMfid
from app.tables import (
    Dataset,
    DeletionAuditLog,
    DeletionRequest,
    Instrument,
    Project,
    Resource,
    Sample,
    User,
)
from app.models.deletion import (
    DeletionAuditScope,
    DeletionAuditLogRead,
    DeletableResourceType,
    DeletionPreviewRead,
    DeletionRequestCapabilities,
    DeletionRequestRead,
    DeletionRequestScope,
    DeletionRequestSort,
    DeletionRequestStatus,
    DeletionReviewDecision,
)
from app.models.pagination import PaginatedResponse
from app.models.query_params import SortDirection
from app.models.users import UserPublicRead
from app.models.response_schemas import DetailResponse
from app.utils.auth_utils import (
    authorize_user,
    caller_row,
    is_caller,
    fetch_acl_rows,
    permission_filter,
    require_permission,
    require_platform_admin,
)
from app.utils.permissions import Permission, Principal, has_permission
from app.utils.cursor_utils import (
    CursorError,
    cursor_primary_value,
    filter_fingerprint,
    new_cursor,
    validate_cursor,
)
from app.utils.sql_utils import touch_resource_modification_time
from app.utils.deletion_utils import deletion_preview_read, deletion_summary
from app.general_utils import get_utc_isoformat

router = APIRouter(tags=["deletion"])


def _resolve_project_refs(
    resource_id: str,
    resource_type: Optional[str],
    session: Session,
) -> tuple[Optional[str], Optional[str]]:
    if resource_type in ("dataset", "sample"):
        model = Dataset if resource_type == "dataset" else Sample
        row = session.exec(
            select(model.project_mfid, Project.project_id)
            .outerjoin(Project, Project.unique_id == model.project_mfid)
            .where(model.unique_id == resource_id)
        ).one_or_none()
        return (row[1], row[0]) if row else (None, None)
    if resource_type == "project":
        project = session.exec(
            select(Project).where(Project.unique_id == resource_id)
        ).one_or_none()
        return (project.project_id, project.unique_id) if project else (None, None)
    return None, None


def _raise_blocked(resource_id: str, resource_type: str, blockers: dict[str, int]) -> None:
    raise HTTPException(
        status_code=409,
        detail={
            "detail": (
                f"Cannot delete {resource_type} '{resource_id}': it still has "
                f"assigned datasets or samples."
            ),
            "blocking": blockers,
        },
    )


def _deletion_summary_read(
    resource_id: str,
    resource_type: str,
    deleted: bool,
    summary: dict,
) -> DeletionPreviewRead:
    return deletion_preview_read(resource_id, resource_type, deleted, summary)


def _write_deletion_record(
    background_tasks: Optional[BackgroundTasks],
    resource_id: str,
    resource_type: str,
    resource_name: Optional[str],
    actor: str,
    summary: Optional[dict],
) -> None:
    """Best-effort final record.json write for a deleted project or instrument.
    Tests block real GCS writes, so a missing client never raises here."""
    if background_tasks is None:
        return

    from app.utils.gcs_record_utils import write_record

    record = {
        "unique_id": resource_id,
        "resource_type": resource_type,
        "resource_name": resource_name,
        "status": "deleted",
        "deletion": summary or {},
    }
    background_tasks.add_task(write_record, resource_id, record, actor, "delete")


def _shown_id(user: Optional[User], stored: Optional[str]) -> Optional[str]:
    """The person's current ID when the row is linked to a user, else the ID stored at the time."""
    return user.unique_id if user is not None else stored


def _person_is(user_alias, id_column, stored_column, value: str):
    return or_(user_alias.unique_id == value, and_(id_column.is_(None), stored_column == value))


def _to_read(
    req: DeletionRequest,
    resource_type: Optional[str],
    project_id: Optional[str],
    project_mfid: Optional[str],
    requester: Optional[User],
    reviewer: Optional[User],
    can_review: bool = False,
    preview: Optional[DeletionPreviewRead] = None,
) -> DeletionRequestRead:
    return DeletionRequestRead(
        **{**req.model_dump(), "requester_id": _shown_id(requester, req.requester_id), "reviewer_id": _shown_id(reviewer, req.reviewer_id)},
        resource_mfid=req.resource_id,
        resource_type=resource_type,
        project_id=project_id,
        project_mfid=project_mfid,
        requester=UserPublicRead.model_validate(requester) if requester else None,
        reviewer=UserPublicRead.model_validate(reviewer) if reviewer else None,
        capabilities=DeletionRequestCapabilities(can_review=can_review),
        preview=preview,
    )


def _audit_to_read(
    audit: DeletionAuditLog,
    requester: Optional[User],
    reviewer: Optional[User],
) -> DeletionAuditLogRead:
    return DeletionAuditLogRead(
        **{**audit.model_dump(), "requester_id": _shown_id(requester, audit.requester_id), "reviewer_id": _shown_id(reviewer, audit.reviewer_id)},
        requester=UserPublicRead.model_validate(requester) if requester else None,
        reviewer=UserPublicRead.model_validate(reviewer) if reviewer else None,
    )


def _review_authority_filter(principal: Principal):
    return permission_filter(
        principal,
        DeletionRequest,
        Permission.DELETE,
        resource_id_column=DeletionRequest.resource_id,
    )


def _deletion_sort_expressions(
    sort: DeletionRequestSort,
    project_id_column,
    requester_user,
):
    if sort is DeletionRequestSort.RESOURCE_NAME:
        return (func.lower(DeletionRequest.resource_name),)
    if sort is DeletionRequestSort.PROJECT_ID:
        return (func.lower(project_id_column),)
    if sort is DeletionRequestSort.REQUESTER_NAME:
        return (
            func.lower(requester_user.last_name),
            func.lower(requester_user.first_name),
            func.lower(requester_user.username),
            func.lower(func.coalesce(requester_user.unique_id, DeletionRequest.requester_id)),
        )
    if sort is DeletionRequestSort.STATUS:
        return (
            case(
                (DeletionRequest.status == DeletionRequestStatus.PENDING.value, 0),
                (DeletionRequest.status == DeletionRequestStatus.APPROVED.value, 1),
                (DeletionRequest.status == DeletionRequestStatus.REJECTED.value, 2),
                else_=3,
            ),
        )
    return (DeletionRequest.request_time,)


def _decode_deletion_cursor_position(
    primary: Optional[str],
    sort: DeletionRequestSort,
) -> tuple[object, ...]:
    expected_length = 4 if sort is DeletionRequestSort.REQUESTER_NAME else 1
    if sort is DeletionRequestSort.REQUEST_TIME:
        return (primary,)
    try:
        position = json.loads(primary) if primary is not None else [None]
    except json.JSONDecodeError as exc:
        raise CursorError("Invalid cursor sort position") from exc
    if not isinstance(position, list) or len(position) != expected_length:
        raise CursorError("Invalid cursor sort position")
    if any(value is not None and not isinstance(value, (str, int)) for value in position):
        raise CursorError("Invalid cursor sort position")
    if sort is DeletionRequestSort.STATUS:
        if not isinstance(position[0], int) or not 0 <= position[0] <= 3:
            raise CursorError("Invalid cursor sort position")
    elif any(value is not None and not isinstance(value, str) for value in position):
        raise CursorError("Invalid cursor sort position")
    return tuple(position)


def _deletion_cursor_predicate(
    sort_expressions,
    position: tuple[object, ...],
    request_id: int,
    direction: SortDirection,
):
    id_comparison = (
        DeletionRequest.id > request_id
        if direction is SortDirection.ASC
        else DeletionRequest.id < request_id
    )
    predicates = []
    equal_prefix = []
    for expression, value in zip(sort_expressions, position):
        if value is None:
            strict_comparison = false()
            equality = expression.is_(None)
        else:
            value_comparison = (
                expression > value
                if direction is SortDirection.ASC
                else expression < value
            )
            strict_comparison = or_(value_comparison, expression.is_(None))
            equality = expression == value
        predicates.append(and_(*equal_prefix, strict_comparison))
        equal_prefix.append(equality)
    predicates.append(and_(*equal_prefix, id_comparison))
    return or_(*predicates)


def _deletion_order_by(sort_expressions, direction: SortDirection):
    if direction is SortDirection.ASC:
        return tuple(expression.asc().nulls_last() for expression in sort_expressions) + (
            DeletionRequest.id.asc(),
        )
    return tuple(expression.desc().nulls_last() for expression in sort_expressions) + (
        DeletionRequest.id.desc(),
    )


def _deletion_cursor_primary(
    sort: DeletionRequestSort,
    position: tuple[object, ...],
):
    if sort is DeletionRequestSort.REQUEST_TIME:
        return position[0]
    return json.dumps(position, ensure_ascii=False, separators=(",", ":"))


@router.get("/deletion_requests", response_model=PaginatedResponse[DeletionRequestRead])
def list_deletion_requests(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    scope: Annotated[
        DeletionRequestScope,
        Query(
            description=(
                "Select accessible, caller-submitted, or currently reviewable requests. "
                "Service accounts without platform-administrator authority cannot review."
            )
        ),
    ] = DeletionRequestScope.ACCESSIBLE,
    status: Optional[DeletionRequestStatus] = None,
    project_mfid: Annotated[
        Optional[str],
        Query(
            description=(
                "Limit requests to resources assigned to this canonical project MFID."
            )
        ),
    ] = None,
    sort: Annotated[
        DeletionRequestSort,
        Query(
            description=(
                "Field used to order deletion requests. String fields are ordered "
                "case-insensitively, nulls are last, and request ID is the final "
                "tie-breaker. requester_name orders by last name, first name, "
                "username, and unique ID. Status order is pending, approved, rejected."
            )
        ),
    ] = DeletionRequestSort.REQUEST_TIME,
    direction: Annotated[
        SortDirection,
        Query(description="Sort direction for deletion requests."),
    ] = SortDirection.DESC,
    limit: int = Query(default=100, ge=1, le=1000),
    cursor: Annotated[
        Optional[str],
        Query(
            min_length=1,
            max_length=4096,
            description="Opaque continuation cursor returned by a previous page.",
        ),
    ] = None,
    offset: int = Query(
        default=0,
        ge=0,
        description="Compatibility offset. A supplied cursor takes precedence.",
    ),
):
    """List deletion requests visible to or actionable by the caller."""
    with Session(get_engine()) as session:
        filters_digest = filter_fingerprint({
            "scope": scope.value,
            "status": status.value if status is not None else None,
            "project_mfid": project_mfid,
            "platform_admin": auth_res.is_platform_admin,
        })
        decoded_cursor = None
        decoded_position = None
        decoded_request_id = None
        if cursor is not None:
            try:
                decoded_cursor = validate_cursor(
                    cursor,
                    kind="deletion_request",
                    sort=sort.value,
                    direction=direction.value,
                    filters=filters_digest,
                    user_id=auth_res.user_id,
                )
                if decoded_cursor is None:
                    raise CursorError("Invalid cursor")
                decoded_position = _decode_deletion_cursor_position(
                    cursor_primary_value(decoded_cursor),
                    sort,
                )
                decoded_request_id = int(decoded_cursor.unique_id)
            except (CursorError, ValueError) as exc:
                raise HTTPException(status_code=422, detail="Invalid cursor") from exc

        review_authority = _review_authority_filter(auth_res)
        can_review = and_(
            DeletionRequest.status == DeletionRequestStatus.PENDING.value,
            review_authority,
        )
        resource_project = aliased(Project)
        project_mfid_column = func.coalesce(
            Dataset.project_mfid,
            Sample.project_mfid,
            resource_project.unique_id,
        )
        record_project = aliased(Project)
        project_id_column = record_project.project_id
        requester_user = aliased(User)
        reviewer_user = aliased(User)
        sort_expressions = _deletion_sort_expressions(
            sort,
            project_id_column,
            requester_user,
        )
        count_query = select(func.count(DeletionRequest.id))
        data_query = (
            select(
                DeletionRequest,
                Resource.resource_type,
                project_id_column.label("project_id"),
                project_mfid_column.label("project_mfid"),
                can_review.label("can_review"),
                requester_user,
                reviewer_user,
                *sort_expressions,
            )
            .outerjoin(Resource, Resource.mfid == DeletionRequest.resource_id)
            .outerjoin(Dataset, Dataset.unique_id == DeletionRequest.resource_id)
            .outerjoin(Sample, Sample.unique_id == DeletionRequest.resource_id)
            .outerjoin(resource_project, resource_project.unique_id == DeletionRequest.resource_id)
            .outerjoin(record_project, record_project.unique_id == project_mfid_column)
            .outerjoin(requester_user, requester_user.id == DeletionRequest.requester_user_id)
            .outerjoin(reviewer_user, reviewer_user.id == DeletionRequest.reviewer_user_id)
        )
        if status is not None:
            count_query = count_query.where(DeletionRequest.status == status.value)
            data_query = data_query.where(DeletionRequest.status == status.value)
        if project_mfid is not None:
            count_query = (
                count_query
                .outerjoin(Dataset, Dataset.unique_id == DeletionRequest.resource_id)
                .outerjoin(Sample, Sample.unique_id == DeletionRequest.resource_id)
                .outerjoin(resource_project, resource_project.unique_id == DeletionRequest.resource_id)
                .where(project_mfid_column == project_mfid)
            )
            data_query = data_query.where(project_mfid_column == project_mfid)

        if scope is DeletionRequestScope.SUBMITTED:
            visible = is_caller(DeletionRequest.requester_user_id, auth_res)
            count_query = count_query.where(visible)
            data_query = data_query.where(visible)
        elif scope is DeletionRequestScope.REVIEWABLE:
            count_query = count_query.where(can_review)
            data_query = data_query.where(can_review)
        elif not auth_res.is_platform_admin:
            visible = (
                is_caller(DeletionRequest.requester_user_id, auth_res)
                | review_authority
            )
            count_query = count_query.where(visible)
            data_query = data_query.where(visible)
        if decoded_cursor is not None:
            data_query = data_query.where(
                _deletion_cursor_predicate(
                    sort_expressions,
                    decoded_position,
                    decoded_request_id,
                    direction,
                )
            )
        elif offset:
            data_query = data_query.offset(offset)

        total = session.exec(count_query).one()
        order_by = _deletion_order_by(sort_expressions, direction)
        rows = session.exec(
            data_query.order_by(*order_by).limit(limit + 1)
        ).all()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = []
        for row in rows:
            (
                req,
                resource_type,
                project_id,
                item_project_mfid,
                row_can_review,
                requester,
                reviewer,
            ) = row[:7]
            items.append(
                _to_read(
                    req,
                    resource_type,
                    project_id,
                    item_project_mfid,
                    requester,
                    reviewer,
                    row_can_review,
                )
            )
        next_cursor = None
        if has_more:
            last_request = rows[-1][0]
            position = tuple(rows[-1][7:])
            next_cursor = new_cursor(
                kind="deletion_request",
                sort=sort.value,
                direction=direction.value,
                primary=_deletion_cursor_primary(sort, position),
                unique_id=str(last_request.id),
                filters=filters_digest,
                user_id=auth_res.user_id,
            )
        return PaginatedResponse(
            total=total,
            limit=limit,
            offset=None if cursor is not None else offset,
            items=items,
            next_cursor=next_cursor,
        )


@router.get("/deletion_requests/{request_id}", response_model=DeletionRequestRead)
def get_deletion_request(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    request_id: int,
):
    """Return a request to its submitter or a caller with deletion-review authority."""
    with Session(get_engine()) as session:
        requester_user = aliased(User)
        reviewer_user = aliased(User)
        row = session.exec(
            select(DeletionRequest, requester_user, reviewer_user)
            .outerjoin(requester_user, requester_user.id == DeletionRequest.requester_user_id)
            .outerjoin(reviewer_user, reviewer_user.id == DeletionRequest.reviewer_user_id)
            .where(DeletionRequest.id == request_id)
        ).one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="Deletion request not found.")
        req, requester, reviewer = row
        acl_rows = fetch_acl_rows(session, req.resource_id)
        has_review_authority = has_permission(
            auth_res, acl_rows, Permission.DELETE
        )
        if not auth_res.has_user_id(req.requester_user_id) and not has_review_authority:
            raise HTTPException(status_code=403, detail="Access denied")
        resource = session.exec(
            select(Resource).where(Resource.mfid == req.resource_id)
        ).one_or_none()
        resource_type = resource.resource_type if resource else None
        project_id, project_mfid = _resolve_project_refs(
            req.resource_id,
            resource_type,
            session,
        )
        preview = None
        if resource_type in ("project", "instrument"):
            summary = deletion_summary(session, resource_type, req.resource_id)
            preview = _deletion_summary_read(req.resource_id, resource_type, False, summary)
        return _to_read(
            req,
            resource_type,
            project_id,
            project_mfid,
            requester,
            reviewer,
            req.status == DeletionRequestStatus.PENDING.value and has_review_authority,
            preview=preview,
        )


@router.patch("/deletion_requests/{request_id}", response_model=DeletionRequestRead)
def review_deletion_request(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    request_id: int,
    status: DeletionReviewDecision,
    reviewer_notes: Optional[str] = None,
    background_tasks: BackgroundTasks = None,
):
    """Review a pending request with effective Admin or Owner access.

    Non-administrator service accounts cannot review deletion requests.
    """
    try:
        decision = DeletionReviewDecision(status)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="status must be 'approved' or 'rejected'.",
        )

    with Session(get_engine()) as session:
        req = session.exec(
            select(DeletionRequest)
            .where(DeletionRequest.id == request_id)
            .with_for_update()
        ).one_or_none()
        if req is None:
            raise HTTPException(status_code=404, detail="Deletion request not found.")
        resource = session.exec(
            select(Resource).where(Resource.mfid == req.resource_id).with_for_update()
        ).one_or_none()
        if req.status == DeletionRequestStatus.REJECTED.value:
            raise HTTPException(
                status_code=409,
                detail="Request has already been rejected and cannot be reviewed again.",
            )
        if (
            req.status == DeletionRequestStatus.APPROVED.value
            and decision is DeletionReviewDecision.APPROVED
        ):
            raise HTTPException(
                status_code=409,
                detail="Request is already approved.",
            )
        resource_type = resource.resource_type if resource else None
        if resource_type not in ("dataset", "sample", "project", "instrument"):
            raise HTTPException(
                status_code=400,
                detail=f"Deletion is not supported for resource type '{resource_type}'.",
            )

        # Approving a deletion is the deletion decision, so it sits at the same
        # bar as performing one: admin on the resource, or platform admin.
        require_permission(session, auth_res, req.resource_id, Permission.DELETE)

        # Computed inside the transaction so data assigned after the request
        # was created still blocks, and so the response reports what this
        # approval actually removed. Blockers stop approval; rejecting the
        # request stays allowed so the resource returns to normal reads.
        summary = None
        if resource_type in ("project", "instrument"):
            entity_model = Project if resource_type == "project" else Instrument
            session.exec(
                select(entity_model).where(entity_model.unique_id == req.resource_id).with_for_update()
            ).one_or_none()
            summary = deletion_summary(session, resource_type, req.resource_id)
        if (
            summary is not None
            and summary["blocking"]
            and decision is DeletionReviewDecision.APPROVED
        ):
            _raise_blocked(req.resource_id, resource_type, summary["blocking"])

        # Update the request record.
        req.status = decision.value
        reviewer = caller_row(session, auth_res)
        req.reviewer_id = reviewer.unique_id
        req.reviewer_user_id = reviewer.id
        req.reviewer_notes = reviewer_notes
        req.review_time = get_utc_isoformat()
        session.add(req)
        project_id, project_mfid = _resolve_project_refs(
            req.resource_id,
            resource_type,
            session,
        )
        if resource_type == "project":
            resource_name = req.resource_name or project_id
        elif resource_type == "instrument":
            instrument = session.exec(
                select(Instrument).where(Instrument.unique_id == req.resource_id)
            ).one_or_none()
            resource_name = req.resource_name or (instrument.instrument_name if instrument else None)
        else:
            resource_name = req.resource_name
        requester = session.get(User, req.requester_user_id) if req.requester_user_id is not None else None

        preview = None
        if summary is not None:
            preview = _deletion_summary_read(
                req.resource_id,
                resource_type,
                decision is DeletionReviewDecision.APPROVED,
                summary,
            )

        snapshot = DeletionRequestRead(
            id=req.id,
            resource_id=req.resource_id,
            resource_mfid=req.resource_id,
            resource_type=resource_type,
            resource_name=resource_name,
            project_id=project_id,
            project_mfid=project_mfid,
            requester_id=_shown_id(requester, req.requester_id),
            requester=UserPublicRead.model_validate(requester) if requester else None,
            reason=req.reason,
            status=req.status,
            request_time=req.request_time,
            review_time=req.review_time,
            reviewer_id=reviewer.unique_id,
            reviewer=UserPublicRead.model_validate(reviewer),
            reviewer_notes=req.reviewer_notes,
            capabilities=DeletionRequestCapabilities(can_review=False),
            preview=preview,
        )

        if decision is DeletionReviewDecision.APPROVED:
            from app.utils.deletion_utils import hard_delete_resource
            audit = DeletionAuditLog(
                resource_id=req.resource_id,
                resource_type=resource_type,
                resource_name=resource_name,
                project_id=project_id,
                project_mfid=project_mfid,
                requester_id=req.requester_id,
                requester_user_id=req.requester_user_id,
                reason=req.reason,
                request_time=req.request_time,
                reviewer_id=reviewer.unique_id,
                reviewer_user_id=reviewer.id,
                reviewer_notes=reviewer_notes,
                review_time=req.review_time,
                deleted_at=get_utc_isoformat(),
            )
            session.add(audit)

            hard_delete_resource(req.resource_id, session, resource)
            session.commit()
            if summary is not None:
                _write_deletion_record(
                    background_tasks,
                    req.resource_id,
                    resource_type,
                    resource_name,
                    reviewer.unique_id,
                    summary,
                )
        else:
            # Rejection makes the resource visible again in filtered list/search
            # results (deletion_filter_for excludes pending/approved requests) —
            # bump modification_time so syncing clients notice it reappeared.
            touch_resource_modification_time(session, req.resource_id)
            session.commit()

        return snapshot


# ---------------------------------------------------------------------------
# DELETE /resources/{resource_id}
# Owner or platform-admin direct deletion with explicit acknowledgement.
# Projects and instruments preview first and delete only with confirm=true.
# ---------------------------------------------------------------------------
@router.delete("/resources/{mfid}", response_model=Union[DeletionPreviewRead, DetailResponse])
def hard_delete_resource_route(
    resource_id: ResourceMfid,
    background_tasks: BackgroundTasks,
    auth_res: Annotated[Principal, Depends(authorize_user)],
    force: bool = Query(
        ...,
        description="Required acknowledgement for direct permanent deletion.",
    ),
    confirm: bool = Query(
        False,
        description=(
            "Required to actually delete a project or instrument. Omitting it "
            "returns a preview and deletes nothing."
        ),
    ),
):
    """
    Permanently delete a resource from the database without a deletion request.
    Requires force=true and resource owner or platform-admin authority.
    Datasets and samples delete immediately. Projects and instruments first
    return a preview; pass confirm=true to execute the deletion, which still
    requires them to be empty. Stored GCS objects are not deleted.
    """
    if force is not True:
        raise HTTPException(status_code=422, detail="Direct deletion requires force=true.")

    with Session(get_engine()) as session:
        actor = caller_row(session, auth_res)
        resource = session.exec(
            select(Resource).where(Resource.mfid == resource_id).with_for_update()
        ).one_or_none()
        if resource is None:
            raise HTTPException(status_code=404, detail="Resource not found")
        if resource.resource_type not in ("dataset", "sample", "project", "instrument"):
            raise HTTPException(
                status_code=400,
                detail=f"Deletion is not supported for resource type '{resource.resource_type}'.",
            )

        require_permission(session, auth_res, resource_id, Permission.HARD_DELETE)

        if resource.resource_type in ("project", "instrument"):
            resource_type = resource.resource_type
            entity_model = Project if resource_type == "project" else Instrument
            entity_query = select(entity_model).where(entity_model.unique_id == resource_id)
            if confirm:
                entity_query = entity_query.with_for_update()
            entity = session.exec(entity_query).one_or_none()
            if entity is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"No {resource_type} row exists for resource '{resource_id}'.",
                )
            summary = deletion_summary(session, resource_type, resource_id)
            if summary["blocking"]:
                _raise_blocked(resource_id, resource_type, summary["blocking"])
            if not confirm:
                return _deletion_summary_read(resource_id, resource_type, False, summary)
            resource_name = entity.project_id if resource_type == "project" else entity.instrument_name

            audit = DeletionAuditLog(
                resource_id=resource_id,
                resource_type=resource_type,
                resource_name=resource_name,
                project_id=resource_name if resource_type == "project" else None,
                project_mfid=resource_id if resource_type == "project" else None,
                requester_id=actor.unique_id,
                requester_user_id=actor.id,
                reason="direct forced deletion",
                request_time=None,
                reviewer_id=actor.unique_id,
                reviewer_user_id=actor.id,
                reviewer_notes="force=True",
                review_time=get_utc_isoformat(),
                deleted_at=get_utc_isoformat(),
            )
            session.add(audit)

            from app.utils.deletion_utils import hard_delete_resource
            hard_delete_resource(resource_id, session, resource)
            session.commit()
            _write_deletion_record(
                background_tasks,
                resource_id,
                resource_type,
                resource_name,
                actor.unique_id,
                summary,
            )
            return _deletion_summary_read(resource_id, resource_type, True, summary)

        # Resolve resource name from the entity before destroying rows
        resource_name = None
        if resource.resource_type == "dataset":
            ds = session.exec(select(Dataset).where(Dataset.unique_id == resource_id)).one_or_none()
            resource_name = ds.dataset_name if ds else None
        elif resource.resource_type == "sample":
            samp = session.exec(select(Sample).where(Sample.unique_id == resource_id)).one_or_none()
            resource_name = samp.sample_name if samp else None

        project_id, project_mfid = _resolve_project_refs(
            resource_id,
            resource.resource_type,
            session,
        )
        audit = DeletionAuditLog(
            resource_id=resource_id,
            resource_type=resource.resource_type,
            resource_name=resource_name,
            project_id=project_id,
            project_mfid=project_mfid,
            requester_id=actor.unique_id,
            requester_user_id=actor.id,
            reason="direct forced deletion",
            request_time=None,
            reviewer_id=actor.unique_id,
            reviewer_user_id=actor.id,
            reviewer_notes="force=True",
            review_time=get_utc_isoformat(),
            deleted_at=get_utc_isoformat(),
        )
        session.add(audit)

        from app.utils.deletion_utils import hard_delete_resource
        hard_delete_resource(resource_id, session, resource)
        session.commit()

    return {"detail": f"Resource {resource_id} permanently deleted"}


# ---------------------------------------------------------------------------
# GET /deletion_audit
# Admin-only. Paginated audit log with optional filters.
# ---------------------------------------------------------------------------
@router.get("/deletion_audit", response_model=PaginatedResponse[DeletionAuditLogRead])
def list_deletion_audit(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    scope: Annotated[
        DeletionAuditScope,
        Query(
            description=(
                "Select the platform-wide administrator audit or deletion history "
                "submitted by the authenticated human user."
            )
        ),
    ] = DeletionAuditScope.ALL,
    resource_id: Annotated[
        Optional[str],
        Query(description="Exact MFID of the deleted resource."),
    ] = None,
    resource_type: Annotated[
        Optional[DeletableResourceType],
        Query(description="Type of deleted resource."),
    ] = None,
    project_id: Annotated[
        Optional[str],
        Query(description="Exact project slug captured when the resource was deleted."),
    ] = None,
    project_mfid: Annotated[
        Optional[str],
        Query(
            description=(
                "Exact canonical project MFID captured when the resource was deleted. "
                "Entries created before project MFID snapshots were introduced do not match."
            )
        ),
    ] = None,
    requester_id: Annotated[
        Optional[str],
        Query(description="Exact stable requester user ID."),
    ] = None,
    reviewer_id: Annotated[
        Optional[str],
        Query(description="Exact stable reviewer user ID."),
    ] = None,
    direction: Annotated[
        SortDirection,
        Query(
            description=(
                "Sort direction for deletion time. Audit ID is the deterministic "
                "final tie-breaker."
            )
        ),
    ] = SortDirection.DESC,
    limit: int = Query(default=100, ge=1, le=1000),
    cursor: Annotated[
        Optional[str],
        Query(
            min_length=1,
            max_length=4096,
            description="Opaque continuation cursor returned by a previous page.",
        ),
    ] = None,
    offset: int = Query(
        default=0,
        ge=0,
        description="Compatibility offset. A supplied cursor takes precedence.",
    ),
):
    """List permanent deletion records visible to the authenticated human user."""
    if auth_res.is_service_account:
        raise HTTPException(status_code=403, detail="Deletion audit is available only to human users")
    if scope is DeletionAuditScope.ALL:
        require_platform_admin(auth_res)
    elif requester_id is not None and requester_id != auth_res.user_id:
        raise HTTPException(status_code=403, detail="Access denied")
    with Session(get_engine()) as session:
        effective_requester_id = (
            auth_res.user_id
            if scope is DeletionAuditScope.SUBMITTED
            else requester_id
        )
        filters_digest = filter_fingerprint({
            "scope": scope.value,
            "resource_id": resource_id,
            "resource_type": resource_type.value if resource_type is not None else None,
            "project_id": project_id,
            "project_mfid": project_mfid,
            "requester_id": effective_requester_id,
            "reviewer_id": reviewer_id,
            "platform_admin": auth_res.is_platform_admin,
        })
        cursor_deleted_at = None
        cursor_audit_id = None
        if cursor is not None:
            try:
                decoded_cursor = validate_cursor(
                    cursor,
                    kind="deletion_audit",
                    sort="deleted_at",
                    direction=direction.value,
                    filters=filters_digest,
                    user_id=auth_res.user_id,
                )
                if decoded_cursor is None:
                    raise CursorError("Invalid cursor")
                cursor_deleted_at = cursor_primary_value(decoded_cursor)
                if cursor_deleted_at is None:
                    raise CursorError("Invalid cursor position")
                cursor_audit_id = int(decoded_cursor.unique_id)
            except (CursorError, ValueError) as exc:
                raise HTTPException(status_code=422, detail="Invalid cursor") from exc

        requester_user = aliased(User)
        reviewer_user = aliased(User)
        def with_people(statement):
            return (
                statement
                .outerjoin(requester_user, requester_user.id == DeletionAuditLog.requester_user_id)
                .outerjoin(reviewer_user, reviewer_user.id == DeletionAuditLog.reviewer_user_id)
            )
        count_query = with_people(select(func.count(DeletionAuditLog.id)).select_from(DeletionAuditLog))
        data_query = with_people(select(DeletionAuditLog, requester_user, reviewer_user))
        if resource_id is not None:
            count_query = count_query.where(DeletionAuditLog.resource_id == resource_id)
            data_query = data_query.where(DeletionAuditLog.resource_id == resource_id)
        if resource_type is not None:
            count_query = count_query.where(DeletionAuditLog.resource_type == resource_type.value)
            data_query = data_query.where(DeletionAuditLog.resource_type == resource_type.value)
        if project_id is not None:
            count_query = count_query.where(DeletionAuditLog.project_id == project_id)
            data_query = data_query.where(DeletionAuditLog.project_id == project_id)
        if project_mfid is not None:
            count_query = count_query.where(DeletionAuditLog.project_mfid == project_mfid)
            data_query = data_query.where(DeletionAuditLog.project_mfid == project_mfid)
        if scope is DeletionAuditScope.SUBMITTED:
            requested_by = is_caller(DeletionAuditLog.requester_user_id, auth_res)
        elif requester_id is not None:
            requested_by = _person_is(requester_user, DeletionAuditLog.requester_user_id, DeletionAuditLog.requester_id, requester_id)
        else:
            requested_by = None
        if requested_by is not None:
            count_query = count_query.where(requested_by)
            data_query = data_query.where(requested_by)
        if reviewer_id is not None:
            reviewed_by = _person_is(reviewer_user, DeletionAuditLog.reviewer_user_id, DeletionAuditLog.reviewer_id, reviewer_id)
            count_query = count_query.where(reviewed_by)
            data_query = data_query.where(reviewed_by)
        if cursor_deleted_at is not None:
            if direction is SortDirection.ASC:
                data_query = data_query.where(or_(
                    DeletionAuditLog.deleted_at > cursor_deleted_at,
                    and_(
                        DeletionAuditLog.deleted_at == cursor_deleted_at,
                        DeletionAuditLog.id > cursor_audit_id,
                    ),
                ))
            else:
                data_query = data_query.where(or_(
                    DeletionAuditLog.deleted_at < cursor_deleted_at,
                    and_(
                        DeletionAuditLog.deleted_at == cursor_deleted_at,
                        DeletionAuditLog.id < cursor_audit_id,
                    ),
                ))
        elif offset:
            data_query = data_query.offset(offset)
        total = session.exec(count_query).one()
        if direction is SortDirection.ASC:
            order_by = (DeletionAuditLog.deleted_at.asc(), DeletionAuditLog.id.asc())
        else:
            order_by = (DeletionAuditLog.deleted_at.desc(), DeletionAuditLog.id.desc())
        rows = session.exec(data_query.order_by(*order_by).limit(limit + 1)).all()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [_audit_to_read(audit, requester, reviewer) for audit, requester, reviewer in rows]
        next_cursor = None
        if has_more:
            last_audit = rows[-1][0]
            next_cursor = new_cursor(
                kind="deletion_audit",
                sort="deleted_at",
                direction=direction.value,
                primary=last_audit.deleted_at,
                unique_id=str(last_audit.id),
                filters=filters_digest,
                user_id=auth_res.user_id,
            )
        return PaginatedResponse(
            total=total,
            limit=limit,
            offset=None if cursor is not None else offset,
            items=items,
            next_cursor=next_cursor,
        )


# ---------------------------------------------------------------------------
# GET /deletion_audit/{audit_id}
# Admin-only. Fetch a single audit record by its integer ID.
# ---------------------------------------------------------------------------
@router.get("/deletion_audit/{audit_id}", response_model=DeletionAuditLogRead)
def get_deletion_audit(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    audit_id: int,
):
    if auth_res.is_service_account:
        raise HTTPException(status_code=403, detail="Deletion audit is available only to human users")
    with Session(get_engine()) as session:
        requester_user = aliased(User)
        reviewer_user = aliased(User)
        row = session.exec(
            select(DeletionAuditLog, requester_user, reviewer_user)
            .outerjoin(requester_user, requester_user.id == DeletionAuditLog.requester_user_id)
            .outerjoin(reviewer_user, reviewer_user.id == DeletionAuditLog.reviewer_user_id)
            .where(DeletionAuditLog.id == audit_id)
        ).one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="Audit log entry not found.")
        if not auth_res.has_user_id(row[0].requester_user_id):
            require_platform_admin(auth_res)
        return _audit_to_read(*row)

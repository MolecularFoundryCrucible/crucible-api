import logging
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

logger = logging.getLogger(__name__)
from sqlalchemy import and_, func, or_
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.models.core_models import (
    Dataset,
    DeletionAuditLog,
    DeletionRequest,
    Resource,
    Sample,
)
from app.models.deletion import (
    DeletionAuditLogRead,
    DeletionRequestCapabilities,
    DeletionRequestRead,
    DeletionRequestScope,
    DeletionRequestSort,
    DeletionRequestStatus,
    DeletionReviewDecision,
)
from app.models.pagination import PaginatedResponse
from app.models.query_params import SortDirection
from app.utils.auth_utils import (
    authorize_user,
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
from app.general_utils import get_utc_isoformat

router = APIRouter(tags=["deletion"])
engine = get_engine()


def _resolve_project_refs(
    resource_id: str,
    resource_type: Optional[str],
    session: Session,
) -> tuple[Optional[str], Optional[str]]:
    if resource_type == "dataset":
        ds = session.exec(
            select(Dataset).where(Dataset.unique_id == resource_id)
        ).one_or_none()
        return (ds.project_id, ds.project_mfid) if ds else (None, None)
    if resource_type == "sample":
        samp = session.exec(
            select(Sample).where(Sample.unique_id == resource_id)
        ).one_or_none()
        return (samp.project_id, samp.project_mfid) if samp else (None, None)
    return None, None


def _to_read(
    req: DeletionRequest,
    resource_type: Optional[str],
    project_id: Optional[str],
    project_mfid: Optional[str],
    can_review: bool = False,
) -> DeletionRequestRead:
    return DeletionRequestRead(
        **req.model_dump(),
        resource_type=resource_type,
        project_id=project_id,
        project_mfid=project_mfid,
        capabilities=DeletionRequestCapabilities(can_review=can_review),
    )


def _review_authority_filter(principal: Principal):
    return permission_filter(
        principal,
        DeletionRequest,
        Permission.DELETE,
        resource_id_column=DeletionRequest.resource_id,
    )


def _deletion_cursor_predicate(
    request_time: Optional[str],
    request_id: int,
    direction: SortDirection,
):
    id_comparison = (
        DeletionRequest.id > request_id
        if direction is SortDirection.ASC
        else DeletionRequest.id < request_id
    )
    if request_time is None:
        return and_(DeletionRequest.request_time.is_(None), id_comparison)
    time_comparison = (
        DeletionRequest.request_time > request_time
        if direction is SortDirection.ASC
        else DeletionRequest.request_time < request_time
    )
    return or_(
        time_comparison,
        and_(DeletionRequest.request_time == request_time, id_comparison),
        DeletionRequest.request_time.is_(None),
    )


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
        Query(description="Field used to order deletion requests."),
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
    with Session(engine) as session:
        filters_digest = filter_fingerprint({
            "scope": scope.value,
            "status": status.value if status is not None else None,
            "project_mfid": project_mfid,
            "platform_admin": auth_res.is_platform_admin,
        })
        decoded_cursor = None
        decoded_request_time = None
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
                decoded_request_time = cursor_primary_value(decoded_cursor)
                decoded_request_id = int(decoded_cursor.unique_id)
            except (CursorError, ValueError) as exc:
                raise HTTPException(status_code=422, detail="Invalid cursor") from exc

        review_authority = _review_authority_filter(auth_res)
        can_review = and_(
            DeletionRequest.status == DeletionRequestStatus.PENDING.value,
            review_authority,
        )
        project_mfid_column = func.coalesce(
            Dataset.project_mfid,
            Sample.project_mfid,
        )
        count_query = select(func.count(DeletionRequest.id))
        data_query = (
            select(
                DeletionRequest,
                Resource.resource_type,
                func.coalesce(Dataset.project_id, Sample.project_id).label("project_id"),
                project_mfid_column.label("project_mfid"),
                can_review.label("can_review"),
            )
            .outerjoin(Resource, Resource.mfid == DeletionRequest.resource_id)
            .outerjoin(Dataset, Dataset.unique_id == DeletionRequest.resource_id)
            .outerjoin(Sample, Sample.unique_id == DeletionRequest.resource_id)
        )
        if status is not None:
            count_query = count_query.where(DeletionRequest.status == status.value)
            data_query = data_query.where(DeletionRequest.status == status.value)
        if project_mfid is not None:
            count_query = (
                count_query
                .outerjoin(Dataset, Dataset.unique_id == DeletionRequest.resource_id)
                .outerjoin(Sample, Sample.unique_id == DeletionRequest.resource_id)
                .where(project_mfid_column == project_mfid)
            )
            data_query = data_query.where(project_mfid_column == project_mfid)

        if scope is DeletionRequestScope.SUBMITTED:
            visible = DeletionRequest.requester_id == auth_res.user_id
            count_query = count_query.where(visible)
            data_query = data_query.where(visible)
        elif scope is DeletionRequestScope.REVIEWABLE:
            count_query = count_query.where(can_review)
            data_query = data_query.where(can_review)
        elif not auth_res.is_platform_admin:
            visible = (
                (DeletionRequest.requester_id == auth_res.user_id)
                | review_authority
            )
            count_query = count_query.where(visible)
            data_query = data_query.where(visible)
        if decoded_cursor is not None:
            data_query = data_query.where(
                _deletion_cursor_predicate(
                    decoded_request_time,
                    decoded_request_id,
                    direction,
                )
            )
        elif offset:
            data_query = data_query.offset(offset)

        total = session.exec(count_query).one()
        order_by = (
            (
                DeletionRequest.request_time.asc().nulls_last(),
                DeletionRequest.id.asc(),
            )
            if direction is SortDirection.ASC
            else (
                DeletionRequest.request_time.desc().nulls_last(),
                DeletionRequest.id.desc(),
            )
        )
        rows = session.exec(
            data_query.order_by(*order_by).limit(limit + 1)
        ).all()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [
            _to_read(
                req,
                resource_type,
                project_id,
                item_project_mfid,
                row_can_review,
            )
            for (
                req,
                resource_type,
                project_id,
                item_project_mfid,
                row_can_review,
            ) in rows
        ]
        next_cursor = None
        if has_more:
            last_request = rows[-1][0]
            next_cursor = new_cursor(
                kind="deletion_request",
                sort=sort.value,
                direction=direction.value,
                primary=last_request.request_time,
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
    with Session(engine) as session:
        req = session.exec(
            select(DeletionRequest)
            .where(DeletionRequest.id == request_id)
        ).one_or_none()
        if req is None:
            raise HTTPException(status_code=404, detail="Deletion request not found.")
        acl_rows = fetch_acl_rows(session, req.resource_id)
        has_review_authority = has_permission(
            auth_res, acl_rows, Permission.DELETE
        )
        if req.requester_id != auth_res.user_id and not has_review_authority:
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
        return _to_read(
            req,
            resource_type,
            project_id,
            project_mfid,
            req.status == DeletionRequestStatus.PENDING.value and has_review_authority,
        )


@router.patch("/deletion_requests/{request_id}", response_model=DeletionRequestRead)
def review_deletion_request(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    request_id: int,
    status: DeletionReviewDecision,
    reviewer_notes: Optional[str] = None,
):
    """Review a pending request with effective Admin or Owner access.

    Non-administrator service accounts cannot review deletion requests.
    """
    user_unique_id = auth_res.user_id
    try:
        decision = DeletionReviewDecision(status)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="status must be 'approved' or 'rejected'.",
        )

    with Session(engine) as session:
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
        if resource_type not in ("dataset", "sample"):
            raise HTTPException(
                status_code=400,
                detail=f"Deletion is not supported for resource type '{resource_type}'.",
            )

        # Approving a deletion is the deletion decision, so it sits at the same
        # bar as performing one: admin on the resource, or platform admin.
        require_permission(session, auth_res, req.resource_id, Permission.DELETE)

        # Update the request record.
        req.status = decision.value
        req.reviewer_id = user_unique_id
        req.reviewer_notes = reviewer_notes
        req.review_time = get_utc_isoformat()
        session.add(req)
        project_id, project_mfid = _resolve_project_refs(
            req.resource_id,
            resource_type,
            session,
        )

        # Snapshot fields before hard delete removes the rows
        snapshot = DeletionRequestRead(
            id=req.id,
            resource_id=req.resource_id,
            resource_type=resource_type,
            resource_name=req.resource_name,
            project_id=project_id,
            project_mfid=project_mfid,
            requester_id=req.requester_id,
            reason=req.reason,
            status=req.status,
            request_time=req.request_time,
            review_time=req.review_time,
            reviewer_id=req.reviewer_id,
            reviewer_notes=req.reviewer_notes,
            capabilities=DeletionRequestCapabilities(can_review=False),
        )

        if decision is DeletionReviewDecision.APPROVED:
            audit = DeletionAuditLog(
                resource_id=req.resource_id,
                resource_type=resource_type,
                resource_name=req.resource_name,
                project_id=project_id,
                requester_id=req.requester_id,
                reason=req.reason,
                request_time=req.request_time,
                reviewer_id=user_unique_id,
                reviewer_notes=reviewer_notes,
                review_time=req.review_time,
                deleted_at=get_utc_isoformat(),
            )
            session.add(audit)

            from app.utils.deletion_utils import hard_delete_resource
            hard_delete_resource(req.resource_id, session, resource)
            session.commit()
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
# ---------------------------------------------------------------------------
@router.delete("/resources/{resource_id}")
def hard_delete_resource_route(
    resource_id: str,
    auth_res: Annotated[Principal, Depends(authorize_user)],
    force: bool = Query(
        ...,
        description="Required acknowledgement for direct permanent deletion.",
    ),
):
    """
    Permanently delete a dataset or sample from the database without a deletion request.
    Requires force=true and resource owner or platform-admin authority.
    Stored GCS objects are not deleted.
    """
    user_unique_id = auth_res.user_id
    if force is not True:
        raise HTTPException(status_code=422, detail="Direct deletion requires force=true.")

    with Session(engine) as session:
        resource = session.exec(
            select(Resource).where(Resource.mfid == resource_id).with_for_update()
        ).one_or_none()
        if resource is None:
            raise HTTPException(status_code=404, detail="Resource not found")
        if resource.resource_type not in ("dataset", "sample"):
            raise HTTPException(
                status_code=400,
                detail=f"Deletion is not supported for resource type '{resource.resource_type}'.",
            )

        require_permission(session, auth_res, resource_id, Permission.HARD_DELETE)

        # Resolve resource name from the entity before destroying rows
        resource_name = None
        if resource.resource_type == "dataset":
            ds = session.exec(select(Dataset).where(Dataset.unique_id == resource_id)).one_or_none()
            resource_name = ds.dataset_name if ds else None
        elif resource.resource_type == "sample":
            samp = session.exec(select(Sample).where(Sample.unique_id == resource_id)).one_or_none()
            resource_name = samp.sample_name if samp else None

        project_id, _ = _resolve_project_refs(
            resource_id,
            resource.resource_type,
            session,
        )
        audit = DeletionAuditLog(
            resource_id=resource_id,
            resource_type=resource.resource_type,
            resource_name=resource_name,
            project_id=project_id,
            requester_id=user_unique_id,
            reason="direct forced deletion",
            request_time=None,
            reviewer_id=user_unique_id,
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
    resource_id: Optional[str] = None,
    requester_id: Optional[str] = None,
    reviewer_id: Optional[str] = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    require_platform_admin(auth_res)
    with Session(engine) as session:
        count_query = select(func.count(DeletionAuditLog.id))
        data_query = select(DeletionAuditLog)
        if resource_id is not None:
            count_query = count_query.where(DeletionAuditLog.resource_id == resource_id)
            data_query = data_query.where(DeletionAuditLog.resource_id == resource_id)
        if requester_id is not None:
            count_query = count_query.where(DeletionAuditLog.requester_id == requester_id)
            data_query = data_query.where(DeletionAuditLog.requester_id == requester_id)
        if reviewer_id is not None:
            count_query = count_query.where(DeletionAuditLog.reviewer_id == reviewer_id)
            data_query = data_query.where(DeletionAuditLog.reviewer_id == reviewer_id)
        total = session.exec(count_query).one()
        items = session.exec(
            data_query.order_by(DeletionAuditLog.deleted_at.desc()).offset(offset).limit(limit)
        ).all()
        return PaginatedResponse(total=total, limit=limit, offset=offset, items=items)


# ---------------------------------------------------------------------------
# GET /deletion_audit/{audit_id}
# Admin-only. Fetch a single audit record by its integer ID.
# ---------------------------------------------------------------------------
@router.get("/deletion_audit/{audit_id}", response_model=DeletionAuditLogRead)
def get_deletion_audit(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    audit_id: int,
):
    require_platform_admin(auth_res)
    with Session(engine) as session:
        entry = session.exec(
            select(DeletionAuditLog).where(DeletionAuditLog.id == audit_id)
        ).one_or_none()
        if entry is None:
            raise HTTPException(status_code=404, detail="Audit log entry not found.")
        return entry

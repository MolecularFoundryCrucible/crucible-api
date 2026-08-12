import logging
from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

logger = logging.getLogger(__name__)
from sqlalchemy import func
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.models.core_models import (
    Dataset, DeletionAuditLog, DeletionRequest, Resource, ResourceACL, Sample)
from app.models.deletion import DeletionAuditLogRead, DeletionRequestRead
from app.models.pagination import PaginatedResponse
from app.utils.auth_utils import authorize_user, require_permission, require_platform_admin
from app.utils.permissions import Permission, Principal, Role, THRESHOLDS
from app.utils.sql_utils import touch_resource_modification_time
from app.general_utils import get_utc_isoformat

router = APIRouter(tags=["deletion"])
engine = get_engine()


def _resolve_project_id(resource_id: str, resource_type: Optional[str], session: Session) -> Optional[str]:
    if resource_type == "dataset":
        ds = session.exec(select(Dataset).where(Dataset.unique_id == resource_id)).one_or_none()
        return ds.project_id if ds else None
    elif resource_type == "sample":
        samp = session.exec(select(Sample).where(Sample.unique_id == resource_id)).one_or_none()
        return samp.project_id if samp else None
    return None


def _to_read(req: DeletionRequest, session: Session) -> DeletionRequestRead:
    """Convert a DeletionRequest (with resource loaded) to its response model."""
    resource_type = req.resource.resource_type if req.resource else None
    return DeletionRequestRead(
        **req.model_dump(),
        resource_type=resource_type,
        project_id=_resolve_project_id(req.resource_id, resource_type, session),
    )


# ---------------------------------------------------------------------------
# GET /deletion_requests
# Admin-only. Returns all requests, optionally filtered by status.
# ---------------------------------------------------------------------------
@router.get("/deletion_requests", response_model=PaginatedResponse[DeletionRequestRead])
def list_deletion_requests(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    status: Optional[str] = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    with Session(engine) as session:
        count_query = select(func.count(DeletionRequest.id))
        data_query = select(DeletionRequest).options(selectinload(DeletionRequest.resource))
        if status is not None:
            count_query = count_query.where(DeletionRequest.status == status)
            data_query = data_query.where(DeletionRequest.status == status)

        # Scoped, not admin-only: you see requests you raised, plus any on a
        # resource you administer. Platform admins see everything.
        if not auth_res.is_platform_admin:
            visible = (
                (DeletionRequest.requester_id == auth_res.user_id)
                | DeletionRequest.resource_id.in_(
                    select(ResourceACL.mfid).where(
                        ResourceACL.accessgroup_id.in_(
                            auth_res.group_ids_at_or_above(THRESHOLDS[Permission.DELETE])),
                        ResourceACL.role.in_(
                            [r.db_value for r in Role
                             if r >= THRESHOLDS[Permission.DELETE]]),
                    )
                )
            )
            count_query = count_query.where(visible)
            data_query = data_query.where(visible)
        total = session.exec(count_query).one()
        items = [_to_read(req, session) for req in session.exec(
            data_query.order_by(DeletionRequest.request_time.desc()).offset(offset).limit(limit)
        ).all()]
        return PaginatedResponse(total=total, limit=limit, offset=offset, items=items)


# ---------------------------------------------------------------------------
# GET /deletion_requests/{request_id}
# Admin-only. Fetch a single request by its integer ID.
# ---------------------------------------------------------------------------
@router.get("/deletion_requests/{request_id}", response_model=DeletionRequestRead)
def get_deletion_request(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    request_id: int,
):
    with Session(engine) as session:
        req = session.exec(
            select(DeletionRequest)
            .options(selectinload(DeletionRequest.resource))
            .where(DeletionRequest.id == request_id)
        ).one_or_none()
        if req is None:
            raise HTTPException(status_code=404, detail="Deletion request not found.")
        # Reviewing a deletion sits at the same bar as performing one.
        require_permission(session, auth_res, req.resource_id, Permission.DELETE)
        return _to_read(req, session)


# ---------------------------------------------------------------------------
# PATCH /deletion_requests/{request_id}
# Admin-only. Approve or reject a pending request.
# ---------------------------------------------------------------------------
@router.patch("/deletion_requests/{request_id}", response_model=DeletionRequestRead)
def review_deletion_request(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    request_id: int,
    status: str,                          # must be "approved" or "rejected"
    reviewer_notes: Optional[str] = None,
):
    user_unique_id, ag_list = auth_res.user_id, auth_res.groups
    if status not in ("approved", "rejected"):
        raise HTTPException(status_code=422, detail="status must be 'approved' or 'rejected'.")

    with Session(engine) as session:
        req = session.exec(
            select(DeletionRequest)
            .options(selectinload(DeletionRequest.resource))
            .where(DeletionRequest.id == request_id)
        ).one_or_none()
        if req is None:
            raise HTTPException(status_code=404, detail="Deletion request not found.")
        if req.status == "rejected":
            raise HTTPException(
                status_code=409,
                detail="Request has already been rejected and cannot be reviewed again.",
            )
        if req.status == "approved" and status == "approved":
            raise HTTPException(
                status_code=409,
                detail="Request is already approved.",
            )

        # Approving a deletion is the deletion decision, so it sits at the same
        # bar as performing one: admin on the resource, or platform admin.
        require_permission(session, auth_res, req.resource_id, Permission.DELETE)

        # Update the request record.
        req.status = status
        req.reviewer_id = user_unique_id
        req.reviewer_notes = reviewer_notes
        req.review_time = get_utc_isoformat()
        session.add(req)

        # Snapshot fields before hard delete removes the rows
        snapshot = DeletionRequestRead(
            id=req.id,
            resource_id=req.resource_id,
            resource_type=req.resource.resource_type if req.resource else None,
            resource_name=req.resource_name,
            requester_id=req.requester_id,
            reason=req.reason,
            status=req.status,
            request_time=req.request_time,
            review_time=req.review_time,
            reviewer_id=req.reviewer_id,
            reviewer_notes=req.reviewer_notes,
        )

        if status == "approved":
            resource_type = req.resource.resource_type if req.resource else None
            # Write a durable audit record before destroying any rows.
            audit = DeletionAuditLog(
                resource_id=req.resource_id,
                resource_type=resource_type,
                resource_name=req.resource_name,
                project_id=_resolve_project_id(req.resource_id, resource_type, session),
                requester_id=req.requester_id,
                reason=req.reason,
                request_time=req.request_time,
                reviewer_id=user_unique_id,
                reviewer_notes=reviewer_notes,
                review_time=req.review_time,
                deleted_at=get_utc_isoformat(),
            )
            session.add(audit)
            session.commit()  # persists both the status update and the audit row

            from app.utils.deletion_utils import hard_delete_resource
            hard_delete_resource(req.resource_id, session)
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
# Admin-only. Hard-delete immediately; requires approved request unless ?force=true.
# ---------------------------------------------------------------------------
@router.delete("/resources/{resource_id}")
def hard_delete_resource_route(
    resource_id: str,
    auth_res: Annotated[Principal, Depends(authorize_user)],
    force: bool = False,
):
    """
    Hard-delete a resource and all related data. Requires admin on the resource.
    Requires an approved deletion request unless ?force=true.
    """
    user_unique_id = auth_res.user_id

    with Session(engine) as session:
        resource = session.exec(select(Resource).where(Resource.mfid == resource_id)).one_or_none()
        if resource is None:
            raise HTTPException(status_code=404, detail="Resource not found")

        require_permission(session, auth_res, resource_id, Permission.DELETE)

        if not force:
            approved = session.exec(
                select(DeletionRequest)
                .where(DeletionRequest.resource_id == resource_id)
                .where(DeletionRequest.status == "approved")
            ).one_or_none()
            if approved is None:
                raise HTTPException(
                    status_code=409,
                    detail="No approved deletion request found for this resource. Submit one via POST /resources/{id}/delete and have it approved, or use ?force=true to bypass.",
                )

        # Resolve resource name from the entity before destroying rows
        resource_name = None
        if resource.resource_type == "dataset":
            ds = session.exec(select(Dataset).where(Dataset.unique_id == resource_id)).one_or_none()
            resource_name = ds.dataset_name if ds else None
        elif resource.resource_type == "sample":
            samp = session.exec(select(Sample).where(Sample.unique_id == resource_id)).one_or_none()
            resource_name = samp.sample_name if samp else None

        audit = DeletionAuditLog(
            resource_id=resource_id,
            resource_type=resource.resource_type,
            resource_name=resource_name,
            project_id=_resolve_project_id(resource_id, resource.resource_type, session),
            requester_id=user_unique_id,
            reason="direct hard delete",
            request_time=None,
            reviewer_id=user_unique_id,
            reviewer_notes="force=True" if force else "approved request exists",
            review_time=get_utc_isoformat(),
            deleted_at=get_utc_isoformat(),
        )
        session.add(audit)
        session.commit()  # persist audit log before destroying rows

        from app.utils.deletion_utils import hard_delete_resource
        hard_delete_resource(resource_id, session)
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

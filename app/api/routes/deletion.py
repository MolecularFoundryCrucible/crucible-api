from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.models.core_models import DeletionRequest
from app.models.deletion import DeletionRequestRead
from app.models.pagination import PaginatedResponse
from app.utils.auth_utils import authorize_user, require_groups

router = APIRouter(tags=["deletion"])
engine = get_engine()


def _to_read(req: DeletionRequest) -> DeletionRequestRead:
    """Convert a DeletionRequest (with resource loaded) to its response model."""
    return DeletionRequestRead(
        **req.model_dump(),
        resource_type=req.resource.resource_type if req.resource else None,
    )


# ---------------------------------------------------------------------------
# GET /deletion_requests
# Admin-only. Returns all requests, optionally filtered by status.
# ---------------------------------------------------------------------------
@router.get("/deletion_requests", response_model=PaginatedResponse[DeletionRequestRead])
def list_deletion_requests(
    auth_res: Annotated[tuple, Depends(authorize_user)],
    status: Optional[str] = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    _, ag_list = auth_res
    require_groups(ag_list)
    with Session(engine) as session:
        count_query = select(func.count(DeletionRequest.id))
        data_query = select(DeletionRequest).options(selectinload(DeletionRequest.resource))
        if status is not None:
            count_query = count_query.where(DeletionRequest.status == status)
            data_query = data_query.where(DeletionRequest.status == status)
        total = session.exec(count_query).one()
        items = [_to_read(req) for req in session.exec(
            data_query.order_by(DeletionRequest.request_time.desc()).offset(offset).limit(limit)
        ).all()]
        return PaginatedResponse(total=total, limit=limit, offset=offset, items=items)


# ---------------------------------------------------------------------------
# GET /deletion_requests/{request_id}
# Admin-only. Fetch a single request by its integer ID.
# ---------------------------------------------------------------------------
@router.get("/deletion_requests/{request_id}", response_model=DeletionRequestRead)
def get_deletion_request(
    auth_res: Annotated[tuple, Depends(authorize_user)],
    request_id: int,
):
    _, ag_list = auth_res
    require_groups(ag_list)
    with Session(engine) as session:
        req = session.exec(
            select(DeletionRequest)
            .options(selectinload(DeletionRequest.resource))
            .where(DeletionRequest.id == request_id)
        ).one_or_none()
        if req is None:
            raise HTTPException(status_code=404, detail="Deletion request not found.")
        return _to_read(req)


# ---------------------------------------------------------------------------
# PATCH /deletion_requests/{request_id}
# Admin-only. Approve or reject a pending request.
# ---------------------------------------------------------------------------
@router.patch("/deletion_requests/{request_id}", response_model=DeletionRequestRead)
def review_deletion_request(
    auth_res: Annotated[tuple, Depends(authorize_user)],
    request_id: int,
    status: str,                          # must be "approved" or "rejected"
    reviewer_notes: Optional[str] = None,
):
    user_unique_id, ag_list = auth_res
    require_groups(ag_list)
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

        # Update the request record.
        req.status = status
        req.reviewer_id = user_unique_id
        req.reviewer_notes = reviewer_notes
        req.review_time = get_tz_isoformat()
        session.add(req)

        session.commit()
        session.refresh(req)
        return _to_read(req)

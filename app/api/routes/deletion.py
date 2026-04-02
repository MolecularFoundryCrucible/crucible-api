from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.general_utils import get_tz_isoformat
from app.models.core_models import Dataset, Sample
from app.models.deletion import DeletionRequest
from app.models.misc import Resource
from app.utils.auth_utils import authorize_user, is_admin

router = APIRouter(tags=["deletion"])
engine = get_engine()


# ---------------------------------------------------------------------------
# POST /deletion_requests
# Any authenticated user can request deletion of a resource they have access to.
# ---------------------------------------------------------------------------
@router.post("/deletion_requests", response_model=DeletionRequest)
def create_deletion_request(
    auth_res: Annotated[tuple, Depends(authorize_user)],
    resource_id: str,
    reason: Optional[str] = None,
):
    # authorize_user returns (orcid_or_group_name, list_of_ag_ids)
    requester_id, _ = auth_res

    with Session(engine) as session:

        # 1. Verify the resource exists in the idtype table.
        #    Every Dataset and Sample gets a row there on creation.
        resource = session.exec(
            select(Resource).where(Resource.mfid == resource_id)
        ).one_or_none()

        if resource is None:
            raise HTTPException(status_code=404, detail=f"Resource {resource_id} not found.")

        # 2. Reject if there is already an active request for this resource.
        #    We don't want duplicate pending requests piling up.
        existing = session.exec(
            select(DeletionRequest)
            .where(DeletionRequest.resource_id == resource_id)
            .where(DeletionRequest.status.in_(["pending", "approved"]))
        ).one_or_none()

        if existing:
            raise HTTPException(
                status_code=409,
                detail=f"A deletion request for {resource_id} already exists (status: {existing.status}).",
            )

        # 3. Look up the human-readable name so admins don't have to fetch the resource separately.
        resource_name = None
        if resource.resource_type == "dataset":
            ds = session.exec(select(Dataset).where(Dataset.unique_id == resource_id)).one_or_none()
            resource_name = ds.dataset_name if ds else None
        elif resource.resource_type == "sample":
            samp = session.exec(select(Sample).where(Sample.unique_id == resource_id)).one_or_none()
            resource_name = samp.sample_name if samp else None

        # 4. Create the DeletionRequest row.
        #    resource_type is copied from idtype so admins know what they are reviewing.
        deletion_req = DeletionRequest(
            resource_type=resource.resource_type,
            resource_id=resource_id,
            resource_name=resource_name,
            requester_id=requester_id,
            reason=reason,
            status="pending",
        )
        session.add(deletion_req)

        session.commit()
        session.refresh(deletion_req)
        return deletion_req


# ---------------------------------------------------------------------------
# GET /deletion_requests
# Admin-only. Returns all requests, optionally filtered by status.
# ---------------------------------------------------------------------------
@router.get("/deletion_requests", response_model=List[DeletionRequest])
def list_deletion_requests(
    admin_id: Annotated[str, Depends(is_admin)],
    status: Optional[str] = None,
):
    with Session(engine) as session:
        query = select(DeletionRequest)
        if status is not None:
            query = query.where(DeletionRequest.status == status)
        return session.exec(query).all()


# ---------------------------------------------------------------------------
# GET /deletion_requests/{request_id}
# Admin-only. Fetch a single request by its integer ID.
# ---------------------------------------------------------------------------
@router.get("/deletion_requests/{request_id}", response_model=DeletionRequest)
def get_deletion_request(
    admin_id: Annotated[str, Depends(is_admin)],
    request_id: int,
):
    with Session(engine) as session:
        req = session.get(DeletionRequest, request_id)
        if req is None:
            raise HTTPException(status_code=404, detail="Deletion request not found.")
        return req


# ---------------------------------------------------------------------------
# PATCH /deletion_requests/{request_id}
# Admin-only. Approve or reject a pending request.
# ---------------------------------------------------------------------------
@router.patch("/deletion_requests/{request_id}", response_model=DeletionRequest)
def review_deletion_request(
    admin_id: Annotated[str, Depends(is_admin)],
    request_id: int,
    status: str,                          # must be "approved" or "rejected"
    reviewer_notes: Optional[str] = None,
):
    if status not in ("approved", "rejected"):
        raise HTTPException(status_code=422, detail="status must be 'approved' or 'rejected'.")

    with Session(engine) as session:
        req = session.get(DeletionRequest, request_id)
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
        req.reviewer_id = admin_id
        req.reviewer_notes = reviewer_notes
        req.review_time = get_tz_isoformat()
        session.add(req)

        session.commit()
        session.refresh(req)
        return req

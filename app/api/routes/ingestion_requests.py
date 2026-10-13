from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.models.core_models import (
    AssociatedFile,
    Dataset,
    IngestionRequest,
)
from app.models.pagination import PaginatedResponse
from app.utils.auth_utils import authorize_user, permission_filter, require_permission
from app.utils.permissions import Permission, Principal
from app.utils.sql_utils import only_update

router = APIRouter(tags=["ingestion_requests"])
engine = get_engine()


def _get_ir_with_access(session: Session, request_id: int, principal: Principal,
                        permission: Permission) -> IngestionRequest:
    """Fetch an IngestionRequest and verify the caller has `permission` on its parent dataset."""
    ir = session.exec(
        select(IngestionRequest).where(IngestionRequest.id == request_id)
    ).one_or_none()
    if ir is None:
        raise HTTPException(status_code=404, detail="Ingestion request not found")

    ds = session.exec(select(Dataset).where(Dataset.unique_id == ir.dataset_mfid)).one_or_none()
    if ds is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    require_permission(session, principal, ds.unique_id, permission)
    return ir


@router.get("/ingestion_requests", response_model=PaginatedResponse[IngestionRequest])
def list_ingestion_requests(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    file_mfid: Optional[str] = None,
    dataset_mfid: Optional[str] = None,
    file_id: Annotated[Optional[str], Query(deprecated=True, description="Deprecated. Use file_mfid.")] = None,
    dataset_id: Annotated[Optional[str], Query(deprecated=True, description="Deprecated. Use dataset_mfid.")] = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    '''List ingestion requests for datasets the caller is authorized to view.

    file_mfid and dataset_mfid are the preferred filters; both accept MFIDs.
    file_id and dataset_id remain as deprecated aliases accepting the same
    values. Passing a file filter together with a dataset filter selects by file.
    '''
    with Session(engine) as session:
        query = (
            select(IngestionRequest)
            .join(Dataset, Dataset.unique_id == IngestionRequest.dataset_mfid)
            .where(permission_filter(auth_res, Dataset, Permission.READ))
        )

        file_mfid = file_mfid or file_id
        dataset_mfid = dataset_mfid or dataset_id

        if file_mfid is not None:
            af = session.exec(
                select(AssociatedFile).where(AssociatedFile.mfid == file_mfid)
            ).one_or_none()
            if af is None:
                raise HTTPException(status_code=404, detail="File not found")

            ds = session.exec(select(Dataset).where(Dataset.id == af.dataset_id)).one_or_none()
            if ds is None:
                raise HTTPException(status_code=404, detail="Dataset not found")
            require_permission(session, auth_res, ds.unique_id, Permission.READ)

            query = query.where(IngestionRequest.file_mfid == af.mfid)

        elif dataset_mfid is not None:
            ds = session.exec(select(Dataset).where(Dataset.unique_id == dataset_mfid)).one_or_none()
            if ds is None:
                raise HTTPException(status_code=404, detail="Dataset not found")
            require_permission(session, auth_res, ds.unique_id, Permission.READ)

            query = query.where(IngestionRequest.dataset_mfid == ds.unique_id)

        total = session.exec(select(func.count()).select_from(query.subquery())).one()
        items = session.exec(
            query.order_by(IngestionRequest.id.desc()).offset(offset).limit(limit)
        ).all()
        return PaginatedResponse(total=total, limit=limit, offset=offset, items=items)


@router.get("/ingestion_requests/{request_id}", response_model=IngestionRequest)
def get_ingestion_request(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    request_id: int,
):
    '''Returns a specific ingestion request. Requires read access to the parent dataset.'''
    with Session(engine) as session:
        return _get_ir_with_access(session, request_id, auth_res, Permission.READ)


@router.patch("/ingestion_requests/{request_id}", response_model=IngestionRequest)
def patch_ingestion_request(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    request_id: int,
    ing_req: IngestionRequest,
):
    '''Updates an ingestion request. Requires edit access to the parent dataset.'''
    if ing_req.id and ing_req.id != request_id:
        raise HTTPException(status_code=400, detail="Request body ID does not match URL")

    with Session(engine) as session:
        ir = _get_ir_with_access(session, request_id, auth_res, Permission.EDIT)

        fields = ing_req.model_dump(exclude_unset=True)
        # Reparenting would move the request under a dataset the caller was never
        # authorized against, so the link is fixed at creation.
        for field in ("dataset_mfid", "file_mfid"):
            if field in fields and fields[field] != getattr(ir, field):
                raise HTTPException(status_code=400, detail=f"{field} cannot be changed")

        updated = only_update(
            session,
            IngestionRequest,
            IngestionRequest.id == request_id,
            **fields,
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="Ingestion request not found")

        session.commit()
        session.refresh(updated)
        return updated

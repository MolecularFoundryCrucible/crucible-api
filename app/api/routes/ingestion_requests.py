from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.tables import Dataset, IngestionRequest
from app.models.ingestion import IngestionRequestRead, IngestionRequestUpdate
from app.models.pagination import PaginatedResponse
from app.utils.auth_utils import authorize_user, permission_filter, require_permission
from app.utils.permissions import Permission, Principal
from app.utils.sql_utils import deletion_filter_for, offset_page, only_update, select_active, select_readable, select_readable_file

router = APIRouter(tags=["ingestion_requests"])


def _get_ir_with_access(session: Session, request_id: int, principal: Principal,
                        permission: Permission) -> IngestionRequest:
    """Fetch an IngestionRequest and verify the caller has `permission` on its parent dataset."""
    ir = session.exec(
        select(IngestionRequest).where(IngestionRequest.id == request_id)
    ).one_or_none()
    if ir is None:
        raise HTTPException(status_code=404, detail="Ingestion request not found")

    if session.exec(select_readable(Dataset, ir.dataset_mfid, Dataset.id, principal=principal)).first() is None:
        raise HTTPException(status_code=404, detail="Ingestion request not found")
    if permission is not Permission.READ:
        require_permission(session, principal, ir.dataset_mfid, permission)
    return ir


@router.get("/ingestion_requests", response_model=PaginatedResponse[IngestionRequestRead])
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
    with Session(get_engine()) as session:
        query = (
            select(IngestionRequest)
            .join(Dataset, Dataset.unique_id == IngestionRequest.dataset_mfid)
            .where(permission_filter(auth_res, Dataset, Permission.READ))
            .where(deletion_filter_for(Dataset))
        )

        file_mfid = file_mfid or file_id
        dataset_mfid = dataset_mfid or dataset_id

        if file_mfid is not None:
            row = session.exec(select_readable_file(file_mfid, auth_res).where(deletion_filter_for(Dataset))).one_or_none()
            if row is None:
                raise HTTPException(status_code=404, detail="File not found")
            query = query.where(IngestionRequest.file_mfid == row[0].mfid)

        elif dataset_mfid is not None:
            if session.exec(select_active(Dataset, dataset_mfid, Dataset.id, principal=auth_res)).first() is None:
                raise HTTPException(status_code=404, detail="Dataset not found")
            query = query.where(IngestionRequest.dataset_mfid == dataset_mfid)

        total, items = offset_page(session, query.order_by(IngestionRequest.id.desc()), limit, offset)
        return PaginatedResponse(total=total, limit=limit, offset=offset, items=items)


@router.get("/ingestion_requests/{request_id}", response_model=IngestionRequestRead)
def get_ingestion_request(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    request_id: int,
):
    '''Returns a specific ingestion request. Requires read access to the parent dataset.'''
    with Session(get_engine()) as session:
        return _get_ir_with_access(session, request_id, auth_res, Permission.READ)


@router.patch("/ingestion_requests/{request_id}", response_model=IngestionRequestRead)
def patch_ingestion_request(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    request_id: int,
    ing_req: IngestionRequestUpdate,
):
    '''Records an ingestion's outcome: `status`, `time_completed` and `ingestion_githash`. Other
    fields in the body are ignored; the dataset and file cannot change. Requires edit access to the
    parent dataset.'''
    if ing_req.id and ing_req.id != request_id:
        raise HTTPException(status_code=400, detail="Request body ID does not match URL")

    with Session(get_engine()) as session:
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

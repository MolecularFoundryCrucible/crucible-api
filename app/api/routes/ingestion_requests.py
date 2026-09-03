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
from app.utils.auth_utils import authorize_user, has_platform_admin_access, require_permission
from app.utils.permissions import Permission, Principal
from app.utils.sql_utils import only_update

router = APIRouter(tags=["ingestion_requests"])
engine = get_engine()


def _get_ir_with_access(session: Session, reqid: int, principal: Principal,
                        permission: Permission) -> IngestionRequest:
    """Fetch an IngestionRequest and verify the caller has `permission` on its parent dataset."""
    ir = session.exec(
        select(IngestionRequest).where(IngestionRequest.id == reqid)
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
    file_id: Optional[str] = None,
    dataset_id: Optional[str] = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    '''
    List ingestion requests, paginated. Requires file_id or dataset_id for non-admins.
    Admins may omit both filters to list all ingestion requests.
    '''
    is_admin = has_platform_admin_access(auth_res)

    if file_id is None and dataset_id is None and not is_admin:
        raise HTTPException(status_code=400, detail="Either file_id or dataset_id must be provided")

    with Session(engine) as session:
        if file_id is not None:
            af = session.exec(
                select(AssociatedFile).where(AssociatedFile.mfid == file_id)
            ).one_or_none()
            if af is None:
                raise HTTPException(status_code=404, detail="File not found")

            ds = session.exec(select(Dataset).where(Dataset.id == af.dataset_id)).one_or_none()
            if ds is None:
                raise HTTPException(status_code=404, detail="Dataset not found")
            require_permission(session, auth_res, ds.unique_id, Permission.READ)

            query = select(IngestionRequest).where(IngestionRequest.file_mfid == af.mfid)

        elif dataset_id is not None:
            ds = session.exec(select(Dataset).where(Dataset.unique_id == dataset_id)).one_or_none()
            if ds is None:
                raise HTTPException(status_code=404, detail="Dataset not found")
            require_permission(session, auth_res, ds.unique_id, Permission.READ)

            query = select(IngestionRequest).where(IngestionRequest.dataset_mfid == ds.unique_id)

        else:
            query = select(IngestionRequest)

        total = session.exec(select(func.count()).select_from(query.subquery())).one()
        items = session.exec(
            query.order_by(IngestionRequest.id.desc()).offset(offset).limit(limit)
        ).all()
        return PaginatedResponse(total=total, limit=limit, offset=offset, items=items)


@router.get("/ingestion_requests/{reqid}", response_model=IngestionRequest)
def get_ingestion_request(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    reqid: int,
):
    '''Returns a specific ingestion request. Requires read access to the parent dataset.'''
    with Session(engine) as session:
        return _get_ir_with_access(session, reqid, auth_res, Permission.READ)


@router.patch("/ingestion_requests/{reqid}", response_model=IngestionRequest)
def patch_ingestion_request(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    reqid: int,
    ing_req: IngestionRequest,
):
    '''Updates an ingestion request. Requires edit access to the parent dataset.'''
    if ing_req.id and ing_req.id != reqid:
        raise HTTPException(status_code=400, detail="Request body ID does not match URL")

    with Session(engine) as session:
        ir = _get_ir_with_access(session, reqid, auth_res, Permission.EDIT)

        fields = ing_req.model_dump(exclude_unset=True)
        # Reparenting would move the request under a dataset the caller was never
        # authorized against, so the link is fixed at creation.
        for field in ("dataset_mfid", "file_mfid"):
            if field in fields and fields[field] != getattr(ir, field):
                raise HTTPException(status_code=400, detail=f"{field} cannot be changed")

        updated = only_update(
            session,
            IngestionRequest,
            IngestionRequest.id == reqid,
            **fields,
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="Ingestion request not found")

        session.commit()
        session.refresh(updated)
        return updated

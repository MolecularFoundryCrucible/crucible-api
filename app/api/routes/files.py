import json
import logging
import os
from typing import Annotated, List, Optional

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, join
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.models.core_models import (
    AssociatedFile,
    Dataset,
    IngestionRequest,
    ResourceACL,
)
from app.models.datasets import AssociatedFileRead, AssociatedFileUpdate
from app.models.pagination import PaginatedResponse
from app.utils.auth_utils import authorize_user, require_write_access

router = APIRouter(tags=["files"])
engine = get_engine()


def _get_af_with_read_access(session: Session, file_id: str, ag_ids: list) -> tuple[AssociatedFile, Dataset]:
    """Fetch an AssociatedFile by mfid and verify the caller has read access to its parent dataset."""
    af = session.exec(select(AssociatedFile).where(AssociatedFile.mfid == file_id)).one_or_none()
    if af is None:
        raise HTTPException(status_code=404, detail="File not found")

    ds = session.exec(
        select(Dataset)
        .select_from(join(Dataset, ResourceACL, ResourceACL.mfid == Dataset.unique_id, isouter=True))
        .where(Dataset.id == af.dataset_id)
        .filter((ResourceACL.accessgroup_id.in_(ag_ids)) | (Dataset.public == True))
        .distinct()
    ).one_or_none()
    if ds is None:
        raise HTTPException(status_code=403, detail="Access denied")
    return af, ds


@router.get("/files", response_model=PaginatedResponse[AssociatedFileRead])
def list_files(
    auth_res: Annotated[tuple, Depends(authorize_user)],
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    '''Returns a paginated list of files the requesting user is authorized to view.'''
    _, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        auth_filter = (ResourceACL.accessgroup_id.in_(ag_ids)) | (Dataset.public == True)
        base_query = (
            select(AssociatedFile)
            .join(Dataset, Dataset.id == AssociatedFile.dataset_id)
            .join(ResourceACL, ResourceACL.mfid == Dataset.unique_id, isouter=True)
            .filter(auth_filter)
            .distinct()
        )
        total = session.exec(
            select(func.count()).select_from(base_query.subquery())
        ).one()
        results = session.exec(base_query.offset(offset).limit(limit)).all()
        return PaginatedResponse(total=total, limit=limit, offset=offset, items=results)


@router.get("/files/{file_id}", response_model=AssociatedFileRead)
def get_file(auth_res: Annotated[tuple, Depends(authorize_user)], file_id: str):
    '''Returns a single file by its MFID.'''
    _, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        af, _ = _get_af_with_read_access(session, file_id, ag_ids)
        return af


@router.patch("/files/{file_id}", response_model=AssociatedFileRead)
def update_file(
    auth_res: Annotated[tuple, Depends(authorize_user)],
    file_id: str,
    updates: AssociatedFileUpdate,
):
    '''Partially updates a file record. Requires write access to the parent dataset.'''
    _, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        af, ds = _get_af_with_read_access(session, file_id, ag_ids)
        require_write_access(session, ag_ids, [ds.unique_id])

        for field, value in updates.model_dump(exclude_unset=True).items():
            setattr(af, field, value)
        session.add(af)
        session.commit()
        session.refresh(af)
        return af


@router.delete("/files/{file_id}")
def delete_file(auth_res: Annotated[tuple, Depends(authorize_user)], file_id: str):
    '''Deletes a file and its ingestion requests. Requires write access to the parent dataset.'''
    _, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        af, ds = _get_af_with_read_access(session, file_id, ag_ids)
        require_write_access(session, ag_ids, [ds.unique_id])

        for ir in session.exec(
            select(IngestionRequest).where(IngestionRequest.file_mfid == af.mfid)
        ).all():
            session.delete(ir)

        session.delete(af)
        session.commit()
        return {"message": f"File {file_id} deleted"}

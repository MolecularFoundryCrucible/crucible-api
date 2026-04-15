from typing import Annotated, Union

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.models.core_models import AccessGroup
from app.models.access_groups import AccessGroupCreate, AccessGroupRead
from app.utils.auth_utils import is_admin

router = APIRouter(tags=["access_groups"])
engine = get_engine()


@router.post("/access_groups", response_model=AccessGroupRead)
def create_access_group(admin_acct: Annotated[str, Depends(is_admin)], ag: AccessGroupCreate):
    '''
    Create a new access group.
    '''
    if not ag.group_name.strip():
        raise HTTPException(status_code=400, detail="group_name must not be empty.")

    with Session(engine) as session:
        db_ag = AccessGroup(group_name=ag.group_name)
        session.add(db_ag)
        session.commit()
        session.refresh(db_ag)
        return db_ag


@router.get("/access_groups/{group_name}", response_model=Union[None, AccessGroupRead])
def read_access_group(admin_acct: Annotated[str, Depends(is_admin)], group_name: str):
    '''
    Returns the access group with the provided name, or None if not found.
    '''
    with Session(engine) as session:
        return session.exec(
            select(AccessGroup).where(AccessGroup.group_name == group_name)
        ).one_or_none()

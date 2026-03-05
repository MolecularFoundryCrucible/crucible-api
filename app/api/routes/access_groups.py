from typing import Annotated, Union

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.models.core_models import AccessGroup
from app.utils.auth_utils import is_admin

router = APIRouter(tags=["access_groups"])
engine = get_engine()


@router.post("/access_groups", response_model=AccessGroup)
def create_access_group(admin_acct: Annotated[str, Depends(is_admin)], ag: AccessGroup):
    """
    Create a new access group in the database.

    Args:
        admin_acct: Admin account identifier
        ag: AccessGroup object to be added

    Returns:
        Added AccessGroup object

    Raises:
        HTTPException: If required access group fields are missing
    """
    # Validate required fields
    missing_fields = []
    if not ag.group_name or not ag.group_name.strip():
        missing_fields.append("group_name")

    if missing_fields:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required access group information: {', '.join(missing_fields)}. Please provide all required fields: group_name."
        )

    with Session(engine) as session:
        session.add(ag)
        session.commit()
    return(ag)


@router.get("/access_groups/{group_name}", response_model=Union[None, AccessGroup])
def read_access_group(admin_acct: Annotated[str, Depends(is_admin)], group_name: str):
    with Session(engine) as session:
        ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == group_name)).one_or_none()
        return(ag)

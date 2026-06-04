from typing import Annotated, List, Union

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.models.core_models import AccessGroup, User, UserAccessGroupLink
from app.models.access_groups import AccessGroupCreate, AccessGroupRead
from app.models.users import UserRead
from app.utils.auth_utils import authorize_user, require_admin, require_groups

router = APIRouter(tags=["access_groups"])
engine = get_engine()


@router.post("/access_groups", response_model=AccessGroupRead)
def create_access_group(auth_res: Annotated[tuple, Depends(authorize_user)], ag: AccessGroupCreate):
    _, ag_list = auth_res
    require_groups(ag_list)
    if not ag.group_name.strip():
        raise HTTPException(status_code=400, detail="group_name must not be empty.")

    with Session(engine) as session:
        db_ag = AccessGroup(group_name=ag.group_name)
        session.add(db_ag)
        session.commit()
        session.refresh(db_ag)
        return db_ag


@router.get("/access_groups/{group_name}", response_model=Union[None, AccessGroupRead])
def read_access_group(auth_res: Annotated[tuple, Depends(authorize_user)], group_name: str):
    _, ag_list = auth_res
    require_groups(ag_list)
    with Session(engine) as session:
        return session.exec(
            select(AccessGroup).where(AccessGroup.group_name == group_name)
        ).one_or_none()


@router.get("/access_groups/{group_name}/users", response_model=List[UserRead])
def list_access_group_users(auth_res: Annotated[tuple, Depends(authorize_user)], group_name: str):
    '''List all users who are members of the given access group. Admin only.'''
    _, ag_list = auth_res
    require_admin(ag_list)

    with Session(engine) as session:
        ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == group_name)).one_or_none()
        if ag is None:
            raise HTTPException(status_code=404, detail="Access group not found")

        return session.exec(
            select(User)
            .join(UserAccessGroupLink, UserAccessGroupLink.user_id == User.id)
            .where(UserAccessGroupLink.accessgroup_id == ag.id)
            .order_by(User.last_name, User.first_name)
        ).all()

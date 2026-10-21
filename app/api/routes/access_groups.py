from typing import Annotated, List, Union

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.api.routes.join_requests import create_join_request
from app.models.core_models import AccessGroup, Instrument, Project, User, UserAccessGroupLink
from app.models.access_groups import AccessGroupCreate, AccessGroupMemberRead, AccessGroupRead
from app.models.join_requests import JoinRequestCreate, JoinRequestRead
from app.utils.auth_utils import authorize_user, require_platform_admin
from app.utils.permissions import AccessGroupType, Principal

router = APIRouter(tags=["access_groups"])
engine = get_engine()


@router.post("/access_groups", response_model=AccessGroupRead)
def create_access_group(auth_res: Annotated[Principal, Depends(authorize_user)], ag: AccessGroupCreate):
    require_platform_admin(auth_res)
    if not ag.group_name.strip():
        raise HTTPException(status_code=400, detail="group_name must not be empty.")

    with Session(engine) as session:
        if ag.group_name in {"admin", "public", "read_only"}:
            raise HTTPException(status_code=409, detail="Reserved system group names cannot be created")
        if session.exec(select(AccessGroup.id).where(AccessGroup.group_name == ag.group_name)).first():
            raise HTTPException(status_code=409, detail="Access group already exists")
        entity_collision = (
            session.exec(select(User.id).where(User.unique_id == ag.group_name)).first()
            or session.exec(select(Project.id).where(Project.unique_id == ag.group_name)).first()
            or session.exec(select(Instrument.id).where(Instrument.unique_id == ag.group_name)).first()
        )
        if entity_collision:
            raise HTTPException(status_code=409, detail="Entity-owned group names cannot be created as custom groups")
        db_ag = AccessGroup(
            group_name=ag.group_name,
            group_type=AccessGroupType.CUSTOM.value,
        )
        session.add(db_ag)
        session.commit()
        session.refresh(db_ag)
        return db_ag


@router.get("/access_groups/{group_name}", response_model=Union[None, AccessGroupRead])
def read_access_group(auth_res: Annotated[Principal, Depends(authorize_user)], group_name: str):
    require_platform_admin(auth_res)
    with Session(engine) as session:
        return session.exec(
            select(AccessGroup).where(AccessGroup.group_name == group_name)
        ).one_or_none()


@router.get("/access_groups/{group_name}/users", response_model=List[AccessGroupMemberRead])
def list_access_group_users(auth_res: Annotated[Principal, Depends(authorize_user)], group_name: str):
    '''List all users who are members of the given access group, with their role. Admin only.'''
    require_platform_admin(auth_res)

    with Session(engine) as session:
        ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == group_name)).one_or_none()
        if ag is None:
            raise HTTPException(status_code=404, detail="Access group not found")

        rows = session.exec(
            select(User, UserAccessGroupLink.role)
            .join(UserAccessGroupLink, UserAccessGroupLink.user_id == User.id)
            .where(UserAccessGroupLink.accessgroup_id == ag.id)
            .order_by(User.last_name, User.first_name)
        ).all()
        return [
            AccessGroupMemberRead.model_validate(user, update={"role": role or "unknown"})
            for user, role in rows
        ]


@router.post("/access_groups/{group_name}/join", response_model=JoinRequestRead, deprecated=True)
def request_to_join_access_group(
    group_name: str,
    body: JoinRequestCreate,
    auth_res: Annotated[Principal, Depends(authorize_user)],
):
    '''Deprecated: use POST /projects/{project_ref}/join_requests. group_name takes a project slug or MFID.'''
    with Session(engine) as session:
        return create_join_request(session, auth_res, group_name, body.reason)

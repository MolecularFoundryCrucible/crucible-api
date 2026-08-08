from typing import Annotated, List, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlmodel import Session, select, col

from app.api.dependencies import get_engine
from app.general_utils import get_utc_isoformat
from app.models.core_models import AccessGroup, AccessGroupJoinRequest, Project, User, UserAccessGroupLink, UserProjectLink
from app.models.access_groups import AccessGroupCreate, AccessGroupRead
from app.models.join_requests import JoinRequestCreate, JoinRequestRead, JoinRequestReview
from app.models.pagination import PaginatedResponse
from app.models.users import UserRead
from app.utils.auth_utils import authorize_user, require_platform_admin
from app.utils.permissions import Principal
from app.utils.project_utils import sql_add_user_to_project

router = APIRouter(tags=["access_groups"])
engine = get_engine()


def _is_group_lead(user_unique_id: str, group_name: str, session: Session) -> bool:
    """True if the caller is the project lead of group_name (only projects have a lead concept for now)."""
    proj = session.exec(select(Project).where(Project.project_id == group_name)).one_or_none()
    return proj is not None and proj.project_lead_orcid == user_unique_id


def _led_group_names(user_unique_id: str, session: Session) -> List[str]:
    """All group_names (project_ids) the caller is the project lead of."""
    return session.exec(
        select(Project.project_id).where(Project.project_lead_orcid == user_unique_id)
    ).all()


@router.post("/access_groups", response_model=AccessGroupRead)
def create_access_group(auth_res: Annotated[Principal, Depends(authorize_user)], ag: AccessGroupCreate):
    require_platform_admin(auth_res)
    if not ag.group_name.strip():
        raise HTTPException(status_code=400, detail="group_name must not be empty.")

    with Session(engine) as session:
        db_ag = AccessGroup(group_name=ag.group_name)
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


@router.get("/access_groups/{group_name}/users", response_model=List[UserRead])
def list_access_group_users(auth_res: Annotated[Principal, Depends(authorize_user)], group_name: str):
    '''List all users who are members of the given access group. Admin only.'''
    require_platform_admin(auth_res)

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


@router.post("/access_groups/{group_name}/join", response_model=JoinRequestRead)
def request_to_join_access_group(
    group_name: str,
    body: JoinRequestCreate,
    auth_res: Annotated[Principal, Depends(authorize_user)],
):
    '''
    Request to join an access group (currently only projects). Blocked if the
    caller is already a member or already has a pending request for this group.
    Resubmission is allowed after a request has been rejected or approved.
    '''
    user_unique_id = auth_res.user_id
    with Session(engine) as session:
        proj = session.exec(select(Project).where(Project.project_id == group_name)).one_or_none()
        if proj is None:
            raise HTTPException(status_code=404, detail=f"Group {group_name} not found")

        user = session.exec(select(User).where(User.unique_id == user_unique_id)).one_or_none()
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")

        existing_membership = session.exec(
            select(UserProjectLink).where(
                UserProjectLink.user_id == user.id,
                UserProjectLink.project_id == group_name,
            )
        ).one_or_none()
        if existing_membership is not None:
            raise HTTPException(status_code=409, detail=f"Already a member of {group_name}.")

        existing_pending = session.exec(
            select(AccessGroupJoinRequest).where(
                AccessGroupJoinRequest.group_name == group_name,
                AccessGroupJoinRequest.requester_id == user_unique_id,
                AccessGroupJoinRequest.status == "pending",
            )
        ).one_or_none()
        if existing_pending is not None:
            raise HTTPException(status_code=409, detail="A pending join request already exists for this group.")

        req = AccessGroupJoinRequest(
            group_name=group_name,
            requester_id=user_unique_id,
            reason=body.reason,
        )
        session.add(req)
        session.commit()
        session.refresh(req)
        return req


@router.get("/join_requests", response_model=PaginatedResponse[JoinRequestRead])
def list_join_requests(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    group_name: Optional[str] = None,
    status: Optional[str] = None,
    requester_id: Optional[str] = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    '''
    List join requests.
    - Admins can list across all groups, optionally filtered by ?group_name=.
    - A non-admin caller who passes ?group_name= must be the lead of that
      group, otherwise 403.
    - A non-admin caller who omits ?group_name= gets requests scoped to every
      group they lead (empty list if they lead none) — this is the cheap way
      for someone leading multiple projects to fetch all their pending
      requests in one call instead of one request per project.
    '''
    user_unique_id, ag_list = auth_res.user_id, auth_res.groups
    is_admin = any(ag.group_name == 'admin' for ag in ag_list)

    with Session(engine) as session:
        query = select(AccessGroupJoinRequest)

        if group_name:
            if not is_admin and not _is_group_lead(user_unique_id, group_name, session):
                raise HTTPException(status_code=403, detail="Only admins, or the lead of the requested group, can list join requests.")
            query = query.where(AccessGroupJoinRequest.group_name == group_name)
        elif not is_admin:
            led_names = _led_group_names(user_unique_id, session)
            if not led_names:
                return PaginatedResponse(total=0, limit=limit, offset=offset, items=[])
            query = query.where(col(AccessGroupJoinRequest.group_name).in_(led_names))

        if status:
            query = query.where(AccessGroupJoinRequest.status == status)
        if requester_id:
            query = query.where(AccessGroupJoinRequest.requester_id == requester_id)

        total = session.exec(select(func.count()).select_from(query.subquery())).one()
        items = session.exec(
            query.order_by(col(AccessGroupJoinRequest.id).desc()).offset(offset).limit(limit)
        ).all()
        return PaginatedResponse(total=total, limit=limit, offset=offset, items=items)


@router.get("/join_requests/{request_id}", response_model=JoinRequestRead)
def get_join_request(
    request_id: int,
    auth_res: Annotated[Principal, Depends(authorize_user)],
):
    '''
    Fetch a single join request by id. Visible to admins, the lead of the
    request's group, or the user who submitted it.
    '''
    user_unique_id, ag_list = auth_res.user_id, auth_res.groups
    is_admin = any(ag.group_name == 'admin' for ag in ag_list)

    with Session(engine) as session:
        req = session.exec(
            select(AccessGroupJoinRequest).where(AccessGroupJoinRequest.id == request_id)
        ).one_or_none()
        if req is None:
            raise HTTPException(status_code=404, detail="Join request not found.")

        if not is_admin and user_unique_id != req.requester_id and not _is_group_lead(user_unique_id, req.group_name, session):
            raise HTTPException(status_code=403, detail="Only admins, the lead of this group, or the requester can view this join request.")

        return req


@router.patch("/join_requests/{request_id}", response_model=JoinRequestRead)
def review_join_request(
    request_id: int,
    body: JoinRequestReview,
    auth_res: Annotated[Principal, Depends(authorize_user)],
):
    '''
    Approve or reject a pending join request. Admin or the lead of the
    request's group only. Approving adds the requester to the group's
    project membership and access group.
    '''
    user_unique_id, ag_list = auth_res.user_id, auth_res.groups
    is_admin = any(ag.group_name == 'admin' for ag in ag_list)

    with Session(engine) as session:
        req = session.exec(
            select(AccessGroupJoinRequest).where(AccessGroupJoinRequest.id == request_id)
        ).one_or_none()
        if req is None:
            raise HTTPException(status_code=404, detail="Join request not found.")

        if not is_admin and not _is_group_lead(user_unique_id, req.group_name, session):
            raise HTTPException(status_code=403, detail="Only admins or the lead of this group can review join requests.")

        if req.status != "pending":
            raise HTTPException(status_code=409, detail=f"Join request is already {req.status}.")

        if body.status == "approved":
            requester = session.exec(select(User).where(User.unique_id == req.requester_id)).one_or_none()
            if requester is None:
                raise HTTPException(status_code=404, detail="Requesting user no longer exists.")
            sql_add_user_to_project(requester, req.group_name, session)

        req.status = body.status
        req.review_time = get_utc_isoformat()
        req.reviewer_id = user_unique_id
        req.reviewer_notes = body.reviewer_notes
        session.add(req)
        session.commit()
        session.refresh(req)
        return req

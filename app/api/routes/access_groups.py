from typing import Annotated, List, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select, col

from app.api.dependencies import get_engine
from app.general_utils import get_utc_isoformat
from app.models.core_models import AccessGroup, AccessGroupJoinRequest, Instrument, Project, User, UserAccessGroupLink
from app.models.access_groups import AccessGroupCreate, AccessGroupMemberRead, AccessGroupRead
from app.models.join_requests import JoinRequestCreate, JoinRequestRead, JoinRequestReview
from app.models.pagination import PaginatedResponse
from app.utils.auth_utils import authorize_user, has_platform_admin_access, require_platform_admin
from app.utils.permissions import AccessGroupType, Principal, Role
from app.utils.project_utils import is_project_member, is_project_owner, sql_add_user_to_project

router = APIRouter(tags=["access_groups"])
engine = get_engine()


def _resolve_group_project_id(accessgroup_id: int, session: Session) -> Optional[str]:
    """The project_id of the project behind this access group, or None if
    accessgroup_id does not name a project's own group."""
    ag = session.exec(select(AccessGroup).where(AccessGroup.id == accessgroup_id)).one_or_none()
    if ag is None or ag.group_type != AccessGroupType.PROJECT.value:
        return None
    proj = session.exec(select(Project).where(Project.unique_id == ag.group_name)).one_or_none()
    return proj.project_id if proj else None


def _to_join_request_read(req: AccessGroupJoinRequest, session: Session) -> JoinRequestRead:
    return JoinRequestRead(
        id=req.id,
        group_name=_resolve_group_project_id(req.accessgroup_id, session),
        requester_id=req.requester_id,
        reason=req.reason,
        status=req.status,
        request_time=req.request_time,
        review_time=req.review_time,
        reviewer_id=req.reviewer_id,
        reviewer_notes=req.reviewer_notes,
    )


def _is_group_lead(user_unique_id: str, accessgroup_id: int, session: Session) -> bool:
    """True if the caller is the project lead of the group behind accessgroup_id
    (only projects have a lead concept for now)."""
    project_id = _resolve_group_project_id(accessgroup_id, session)
    if project_id is None:
        return False
    return is_project_owner(user_unique_id, project_id, session)


def _led_accessgroup_ids(user_unique_id: str, session: Session) -> List[int]:
    """Ids of the access groups of every project the caller holds owner standing in."""
    user = session.exec(select(User).where(User.unique_id == user_unique_id)).one_or_none()
    if user is None:
        return []
    return session.exec(
        select(AccessGroup.id)
        .join(Project, AccessGroup.group_name == Project.unique_id)
        .join(UserAccessGroupLink, UserAccessGroupLink.accessgroup_id == AccessGroup.id)
        .where(
            AccessGroup.group_type == AccessGroupType.PROJECT.value,
            UserAccessGroupLink.user_id == user.id,
            UserAccessGroupLink.role == Role.OWNER.db_value,
        )
    ).all()


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

        ag = session.exec(select(AccessGroup).where(
            AccessGroup.group_name == proj.unique_id,
            AccessGroup.group_type == AccessGroupType.PROJECT.value,
        )).one_or_none()
        if ag is None:
            raise HTTPException(status_code=409, detail=f"No access group for project '{group_name}'")

        user = session.exec(select(User).where(User.unique_id == user_unique_id)).one_or_none()
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")

        if is_project_member(user.id, group_name, session):
            raise HTTPException(status_code=409, detail=f"Already a member of {group_name}.")

        existing_pending = session.exec(
            select(AccessGroupJoinRequest).where(
                AccessGroupJoinRequest.accessgroup_id == ag.id,
                AccessGroupJoinRequest.requester_id == user_unique_id,
                AccessGroupJoinRequest.status == "pending",
            )
        ).one_or_none()
        if existing_pending is not None:
            raise HTTPException(status_code=409, detail="A pending join request already exists for this group.")

        req = AccessGroupJoinRequest(
            accessgroup_id=ag.id,
            requester_id=user_unique_id,
            reason=body.reason,
        )
        session.add(req)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            duplicate = session.exec(
                select(AccessGroupJoinRequest.id).where(
                    AccessGroupJoinRequest.accessgroup_id == ag.id,
                    AccessGroupJoinRequest.requester_id == user_unique_id,
                    AccessGroupJoinRequest.status == "pending",
                )
            ).first()
            if duplicate is not None:
                raise HTTPException(status_code=409, detail="A pending join request already exists for this group.")
            raise
        session.refresh(req)
        return _to_join_request_read(req, session)


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
    user_unique_id = auth_res.user_id
    is_admin = has_platform_admin_access(auth_res)

    with Session(engine) as session:
        query = select(AccessGroupJoinRequest)

        if group_name:
            proj = session.exec(select(Project).where(Project.project_id == group_name)).one_or_none()
            ag = session.exec(select(AccessGroup).where(
                AccessGroup.group_name == proj.unique_id,
                AccessGroup.group_type == AccessGroupType.PROJECT.value,
            )).one_or_none() if proj else None
            if not is_admin and not (ag and _is_group_lead(user_unique_id, ag.id, session)):
                raise HTTPException(status_code=403, detail="Only admins, or the lead of the requested group, can list join requests.")
            if ag is None:
                return PaginatedResponse(total=0, limit=limit, offset=offset, items=[])
            query = query.where(AccessGroupJoinRequest.accessgroup_id == ag.id)
        elif not is_admin:
            led_ids = _led_accessgroup_ids(user_unique_id, session)
            if not led_ids:
                return PaginatedResponse(total=0, limit=limit, offset=offset, items=[])
            query = query.where(col(AccessGroupJoinRequest.accessgroup_id).in_(led_ids))

        if status:
            query = query.where(AccessGroupJoinRequest.status == status)
        if requester_id:
            query = query.where(AccessGroupJoinRequest.requester_id == requester_id)

        total = session.exec(select(func.count()).select_from(query.subquery())).one()
        rows = session.exec(
            query.order_by(col(AccessGroupJoinRequest.id).desc()).offset(offset).limit(limit)
        ).all()
        items = [_to_join_request_read(req, session) for req in rows]
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
    user_unique_id = auth_res.user_id
    is_admin = has_platform_admin_access(auth_res)

    with Session(engine) as session:
        req = session.exec(
            select(AccessGroupJoinRequest).where(AccessGroupJoinRequest.id == request_id)
        ).one_or_none()
        if req is None:
            raise HTTPException(status_code=404, detail="Join request not found.")

        if not is_admin and user_unique_id != req.requester_id and not _is_group_lead(user_unique_id, req.accessgroup_id, session):
            raise HTTPException(status_code=403, detail="Only admins, the lead of this group, or the requester can view this join request.")

        return _to_join_request_read(req, session)


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
    user_unique_id = auth_res.user_id
    is_admin = has_platform_admin_access(auth_res)

    with Session(engine) as session:
        req = session.exec(
            select(AccessGroupJoinRequest)
            .where(AccessGroupJoinRequest.id == request_id)
            .with_for_update()
        ).one_or_none()
        if req is None:
            raise HTTPException(status_code=404, detail="Join request not found.")

        if not is_admin and not _is_group_lead(user_unique_id, req.accessgroup_id, session):
            raise HTTPException(status_code=403, detail="Only admins or the lead of this group can review join requests.")

        if req.status != "pending":
            raise HTTPException(status_code=409, detail=f"Join request is already {req.status}.")

        if body.status == "approved":
            requester = session.exec(select(User).where(User.unique_id == req.requester_id)).one_or_none()
            if requester is None:
                raise HTTPException(status_code=404, detail="Requesting user no longer exists.")
            project_id = _resolve_group_project_id(req.accessgroup_id, session)
            sql_add_user_to_project(requester, project_id, session)

        req.status = body.status
        req.review_time = get_utc_isoformat()
        req.reviewer_id = user_unique_id
        req.reviewer_notes = body.reviewer_notes
        session.add(req)
        session.commit()
        session.refresh(req)
        return _to_join_request_read(req, session)

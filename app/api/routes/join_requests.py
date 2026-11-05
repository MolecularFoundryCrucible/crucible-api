import json
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, case, false, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.api.path_params import ProjectRef
from app.general_utils import get_utc_datetime
from app.models.core_models import AccessGroup, AccessGroupJoinRequest, DeletionRequest, Project, User
from app.models.facets import FacetBucket, FacetResponse, ProjectFacetBucket, RequesterFacetBucket
from app.models.join_requests import (
    JoinRequestCapabilities, JoinRequestCreate, JoinRequestFacetField, JoinRequestRead, JoinRequestReview,
    JoinRequestRole, JoinRequestScope, JoinRequestSort, JoinRequestStatus,
)
from app.models.pagination import PaginatedResponse
from app.models.projects import ProjectReferenceRead
from app.models.query_params import SortDirection
from app.models.users import UserPublicRead
from app.utils.auth_utils import caller_row, authorize_user, permission_filter, require_grant
from app.utils.cursor_utils import CursorError, cursor_primary_value, filter_fingerprint, new_cursor, validate_cursor
from app.utils.permissions import AccessGroupType, Permission, Principal, Role
from app.utils.project_utils import is_project_member, resolve_project_reference, sql_add_user_to_project
from app.models.timestamps import UtcDatetime

router = APIRouter(tags=["join_requests"])

MAX_PENDING_PER_REQUESTER = 20
PENDING = JoinRequestStatus.PENDING.value
STATUS_ORDER = {status.value: position for position, status in enumerate(JoinRequestStatus)}
TIME_SORTS = {JoinRequestSort.REQUEST_TIME, JoinRequestSort.REVIEW_TIME}

requester_user = aliased(User)
reviewer_user = aliased(User)


def _error(status_code: int, code: str, message: str, **extra) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message, **extra})


@dataclass(frozen=True)
class JoinRequestFilters:
    scope: JoinRequestScope
    status: Optional[list[JoinRequestStatus]]
    project_mfid: Optional[list[str]]
    group_name: Optional[str]
    requester_id: Optional[str]
    reviewer_id: Optional[str]
    requested_after: Optional[datetime]
    requested_before: Optional[datetime]
    reviewed_after: Optional[datetime]
    reviewed_before: Optional[datetime]
    q: Optional[str]

    def fingerprint(self, principal: Principal) -> str:
        def iso(value):
            return value.isoformat() if value else None
        return filter_fingerprint({
            "scope": self.scope.value,
            "status": sorted(status.value for status in self.status or []),
            "project_mfid": sorted(self.project_mfid or []),
            "group_name": self.group_name,
            "requester_id": self.requester_id,
            "reviewer_id": self.reviewer_id,
            "requested_after": iso(self.requested_after),
            "requested_before": iso(self.requested_before),
            "reviewed_after": iso(self.reviewed_after),
            "reviewed_before": iso(self.reviewed_before),
            "q": self.q,
            "platform_admin": principal.is_platform_admin,
        })


def join_request_filters(
    scope: Annotated[JoinRequestScope, Query(description="accessible: your own requests and those you may review; submitted: your own; reviewable: those you may review.")] = JoinRequestScope.ACCESSIBLE,
    status: Annotated[Optional[list[JoinRequestStatus]], Query(description="Repeat to match several statuses.")] = None,
    project_mfid: Annotated[Optional[list[str]], Query(description="Repeat to match several projects.")] = None,
    group_name: Annotated[Optional[str], Query(deprecated=True, description="Deprecated. Use project_mfid. Accepts a project slug or MFID.")] = None,
    requester_id: Optional[str] = None,
    reviewer_id: Optional[str] = None,
    requested_after: Optional[UtcDatetime] = None,
    requested_before: Optional[UtcDatetime] = None,
    reviewed_after: Annotated[Optional[UtcDatetime], Query(description="Closed at or after this time (reviewed, withdrawn, or closed by a direct add).")] = None,
    reviewed_before: Optional[UtcDatetime] = None,
    q: Annotated[Optional[str], Query(min_length=1, max_length=200, description="Matches the requester's name, username, or ORCID, case-insensitively.")] = None,
) -> JoinRequestFilters:
    return JoinRequestFilters(scope, status, project_mfid, group_name, requester_id, reviewer_id,
                              requested_after, requested_before, reviewed_after, reviewed_before, q)


def _review_authority(principal: Principal):
    if principal.is_service_account and not principal.is_platform_admin:
        return false()
    return permission_filter(principal, AccessGroup, Permission.EDIT, resource_id_column=AccessGroup.group_name)


def _joined(statement):
    return (
        statement
        .join(AccessGroup, AccessGroup.id == AccessGroupJoinRequest.accessgroup_id)
        .outerjoin(Project, Project.unique_id == AccessGroup.group_name)
        .outerjoin(requester_user, requester_user.id == AccessGroupJoinRequest.requester_user_id)
        .outerjoin(reviewer_user, reviewer_user.id == AccessGroupJoinRequest.reviewer_user_id)
    )


def _conditions(session, principal: Principal, filters: JoinRequestFilters, authority) -> list:
    mine = AccessGroupJoinRequest.requester_user_id == principal.internal_user_id
    if filters.scope is JoinRequestScope.SUBMITTED:
        conditions = [mine]
    elif filters.scope is JoinRequestScope.REVIEWABLE:
        conditions = [authority]
    elif principal.is_platform_admin:
        conditions = []
    else:
        conditions = [or_(mine, authority)]
    if filters.status:
        conditions.append(AccessGroupJoinRequest.status.in_([status.value for status in filters.status]))
    if filters.project_mfid:
        conditions.append(AccessGroup.group_name.in_(filters.project_mfid))
    if filters.group_name:
        project = resolve_project_reference(filters.group_name, session)
        conditions.append(AccessGroup.group_name == project.unique_id if project else false())
    if filters.requester_id:
        conditions.append(requester_user.unique_id == filters.requester_id)
    if filters.reviewer_id:
        conditions.append(reviewer_user.unique_id == filters.reviewer_id)
    if filters.requested_after:
        conditions.append(AccessGroupJoinRequest.request_time >= filters.requested_after)
    if filters.requested_before:
        conditions.append(AccessGroupJoinRequest.request_time <= filters.requested_before)
    if filters.reviewed_after:
        conditions.append(AccessGroupJoinRequest.review_time >= filters.reviewed_after)
    if filters.reviewed_before:
        conditions.append(AccessGroupJoinRequest.review_time <= filters.reviewed_before)
    if filters.q:
        needle = filters.q.strip().lower()
        full_name = func.lower(func.coalesce(requester_user.first_name, "") + " " + func.coalesce(requester_user.last_name, ""))
        conditions.append(or_(
            full_name.contains(needle, autoescape=True),
            func.lower(requester_user.username).contains(needle, autoescape=True),
            func.lower(requester_user.unique_id).contains(needle, autoescape=True),
        ))
    return conditions


def _row_query(principal: Principal):
    authority = _review_authority(principal)
    return _joined(select(
        AccessGroupJoinRequest, AccessGroup.group_name, Project, requester_user, reviewer_user, authority.label("may_review"),
    ).select_from(AccessGroupJoinRequest)), authority


def _read(principal: Principal, req, project_mfid, project, requester, reviewer, may_review) -> JoinRequestRead:
    pending = req.status == PENDING
    return JoinRequestRead(
        id=req.id,
        status=req.status,
        project_mfid=project_mfid,
        project=ProjectReferenceRead.model_validate(project) if project else None,
        requester_id=requester.unique_id if requester else None,
        requester=UserPublicRead.model_validate(requester) if requester else None,
        reason=req.reason,
        request_time=req.request_time,
        reviewer_id=reviewer.unique_id if reviewer else None,
        reviewer=UserPublicRead.model_validate(reviewer) if reviewer else None,
        review_time=req.review_time,
        reviewer_notes=req.reviewer_notes,
        granted_role=req.granted_role,
        capabilities=JoinRequestCapabilities(
            can_review=pending and bool(may_review),
            can_withdraw=pending and req.requester_user_id == principal.internal_user_id,
        ),
        group_name=project.project_id if project else project_mfid,
    )


def _visible_row(session, principal: Principal, request_id: int):
    query, _ = _row_query(principal)
    row = session.exec(query.where(AccessGroupJoinRequest.id == request_id)).first()
    if row is None:
        raise _error(404, "join_request_not_found", "Join request not found")
    req, _, _, _, _, may_review = row
    if not (principal.is_platform_admin or may_review or req.requester_user_id == principal.internal_user_id):
        raise _error(404, "join_request_not_found", "Join request not found")
    return row


def _sort_expressions(sort: JoinRequestSort):
    if sort is JoinRequestSort.REVIEW_TIME:
        return (AccessGroupJoinRequest.review_time,)
    if sort is JoinRequestSort.STATUS:
        return (case(*((AccessGroupJoinRequest.status == value, position) for value, position in STATUS_ORDER.items()), else_=len(STATUS_ORDER)),)
    if sort is JoinRequestSort.PROJECT:
        return (func.lower(func.coalesce(Project.project_id, AccessGroup.group_name)),)
    if sort is JoinRequestSort.REQUESTER_NAME:
        return (
            func.lower(requester_user.last_name),
            func.lower(requester_user.first_name),
            func.lower(requester_user.username),
            func.lower(requester_user.unique_id),
        )
    return (AccessGroupJoinRequest.request_time,)


def _encode_position(position) -> str:
    return json.dumps([value.isoformat() if isinstance(value, datetime) else value for value in position], separators=(",", ":"))


def _decode_position(primary: str, sort: JoinRequestSort, width: int) -> tuple:
    try:
        position = json.loads(primary)
    except (TypeError, json.JSONDecodeError) as exc:
        raise CursorError("Invalid cursor sort position") from exc
    if not isinstance(position, list) or len(position) != width:
        raise CursorError("Invalid cursor sort position")
    if sort in TIME_SORTS:
        try:
            return tuple(None if value is None else datetime.fromisoformat(value) for value in position)
        except (TypeError, ValueError) as exc:
            raise CursorError("Invalid cursor sort position") from exc
    expected = int if sort is JoinRequestSort.STATUS else str
    if any(value is not None and not isinstance(value, expected) for value in position):
        raise CursorError("Invalid cursor sort position")
    return tuple(position)


def _after(sort_expressions, position, request_id: int, direction: SortDirection):
    ascending = direction is SortDirection.ASC
    predicates, equal_prefix = [], []
    for expression, value in zip(sort_expressions, position):
        if value is None:
            strict, equality = false(), expression.is_(None)
        else:
            strict = or_(expression > value if ascending else expression < value, expression.is_(None))
            equality = expression == value
        predicates.append(and_(*equal_prefix, strict))
        equal_prefix.append(equality)
    predicates.append(and_(*equal_prefix, AccessGroupJoinRequest.id > request_id if ascending else AccessGroupJoinRequest.id < request_id))
    return or_(*predicates)


def _order_by(sort_expressions, direction: SortDirection):
    if direction is SortDirection.ASC:
        return [expression.asc().nulls_last() for expression in sort_expressions] + [AccessGroupJoinRequest.id.asc()]
    return [expression.desc().nulls_last() for expression in sort_expressions] + [AccessGroupJoinRequest.id.desc()]


def list_join_requests_page(session, principal: Principal, filters: JoinRequestFilters, sort: JoinRequestSort,
                            direction: SortDirection, limit: int, cursor: Optional[str], offset: int):
    digest = filters.fingerprint(principal)
    sort_expressions = _sort_expressions(sort)
    position = request_id = None
    if cursor is not None:
        try:
            decoded = validate_cursor(cursor, kind="join_request", sort=sort.value, direction=direction.value, filters=digest, user_id=principal.user_id)
            if decoded is None:
                raise CursorError("Invalid cursor")
            position = _decode_position(cursor_primary_value(decoded), sort, len(sort_expressions))
            request_id = int(decoded.unique_id)
        except (CursorError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="Invalid cursor") from exc

    query, authority = _row_query(principal)
    conditions = _conditions(session, principal, filters, authority)
    total = session.exec(_joined(select(func.count(AccessGroupJoinRequest.id)).select_from(AccessGroupJoinRequest)).where(*conditions)).one()
    query = query.add_columns(*sort_expressions).where(*conditions)
    if position is not None:
        query = query.where(_after(sort_expressions, position, request_id, direction))
    elif offset:
        query = query.offset(offset)
    rows = session.exec(query.order_by(*_order_by(sort_expressions, direction)).limit(limit + 1)).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if has_more:
        last = rows[-1]
        next_cursor = new_cursor(kind="join_request", sort=sort.value, direction=direction.value,
                                 primary=_encode_position(tuple(last[6:])), unique_id=str(last[0].id),
                                 filters=digest, user_id=principal.user_id)
    return PaginatedResponse(
        total=total,
        limit=limit,
        offset=None if cursor is not None else offset,
        items=[_read(principal, *row[:6]) for row in rows],
        next_cursor=next_cursor,
    )


def create_join_request(session, principal: Principal, project_ref: str, reason: Optional[str]) -> JoinRequestRead:
    if principal.is_service_account:
        raise _error(403, "service_account_cannot_request", "Service accounts cannot request to join a project; ask a project editor to add it")
    project = resolve_project_reference(project_ref, session)
    if project is None:
        raise _error(404, "project_not_found", f"Project '{project_ref}' not found")
    group = session.exec(select(AccessGroup).where(
        AccessGroup.group_name == project.unique_id,
        AccessGroup.group_type == AccessGroupType.PROJECT.value,
    )).one_or_none()
    if group is None:
        raise _error(409, "project_not_accepting_requests", f"Project '{project.project_id}' has no membership group")
    if session.exec(select(DeletionRequest.id).where(DeletionRequest.resource_id == project.unique_id, DeletionRequest.status.in_(["pending", "approved"]))).first():
        raise _error(409, "project_not_accepting_requests", f"Project '{project.project_id}' is pending deletion")
    user = caller_row(session, principal)
    if user is None:
        raise _error(404, "user_not_found", "User not found")
    if is_project_member(user.id, project.project_id, session):
        raise _error(409, "already_member", f"Already a member of {project.project_id}")

    def pending(*conditions):
        return session.exec(select(AccessGroupJoinRequest.id).where(
            AccessGroupJoinRequest.requester_user_id == user.id, AccessGroupJoinRequest.status == PENDING, *conditions))

    existing = pending(AccessGroupJoinRequest.accessgroup_id == group.id).first()
    if existing is not None:
        raise _error(409, "join_request_pending", "A pending join request already exists for this project", request_id=existing)
    if len(pending().all()) >= MAX_PENDING_PER_REQUESTER:
        raise _error(409, "too_many_pending_requests", f"At most {MAX_PENDING_PER_REQUESTER} join requests can be pending at once; withdraw one first")

    req = AccessGroupJoinRequest(accessgroup_id=group.id, requester_id=user.unique_id, requester_user_id=user.id, reason=reason)
    session.add(req)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise _error(409, "join_request_pending", "A pending join request already exists for this project") from None
    return _read(principal, *_visible_row(session, principal, req.id))


@router.post("/projects/{project_ref}/join_requests", response_model=JoinRequestRead, status_code=201,
             responses={403: {"description": "Service accounts cannot request (`service_account_cannot_request`)"},
                        409: {"description": "`already_member`, `join_request_pending`, `too_many_pending_requests`, or `project_not_accepting_requests`"}})
def request_to_join_project(project_ref: ProjectRef, body: JoinRequestCreate, auth_res: Annotated[Principal, Depends(authorize_user)]):
    '''Ask to join a project. One pending request per project, at most 20 pending in total.'''
    with Session(get_engine()) as session:
        return create_join_request(session, auth_res, project_ref, body.reason)


@router.get("/join_requests", response_model=PaginatedResponse[JoinRequestRead])
def list_join_requests(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    filters: Annotated[JoinRequestFilters, Depends(join_request_filters)],
    sort: Annotated[JoinRequestSort, Query(description="Text sorts are case-insensitive; nulls are last and the request id breaks ties. Status order is pending, approved, rejected, withdrawn. requester_name orders by last name, first name, username, and ORCID.")] = JoinRequestSort.REQUEST_TIME,
    direction: SortDirection = SortDirection.DESC,
    limit: int = Query(default=100, ge=1, le=1000),
    cursor: Annotated[Optional[str], Query(min_length=1, max_length=4096, description="Opaque continuation cursor from next_cursor.")] = None,
    offset: int = Query(default=0, ge=0, description="Compatibility offset. A supplied cursor takes precedence."),
):
    '''List join requests you made or may review. Platform administrators see all of them.'''
    with Session(get_engine()) as session:
        return list_join_requests_page(session, auth_res, filters, sort, direction, limit, cursor, offset)


@router.get("/join_requests/facets", response_model=FacetResponse)
def join_request_facets(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    filters: Annotated[JoinRequestFilters, Depends(join_request_filters)],
    field: JoinRequestFacetField,
    limit: int = Query(default=50, ge=1, le=200),
):
    '''Count join requests by status, project, or requester, with the same scope and filters as GET /join_requests. Buckets are ordered by count, largest first, then by label.'''
    with Session(get_engine()) as session:
        authority = _review_authority(auth_res)
        conditions = _conditions(session, auth_res, filters, authority)
        column = {
            JoinRequestFacetField.STATUS: AccessGroupJoinRequest.status,
            JoinRequestFacetField.PROJECT: AccessGroup.group_name,
            JoinRequestFacetField.REQUESTER: requester_user.unique_id,
        }[field]
        tie_break = {
            JoinRequestFacetField.STATUS: AccessGroupJoinRequest.status,
            JoinRequestFacetField.PROJECT: func.lower(func.coalesce(Project.title, Project.project_id, AccessGroup.group_name)),
            JoinRequestFacetField.REQUESTER: func.lower(func.coalesce(requester_user.first_name, "") + " " + func.coalesce(requester_user.last_name, "")),
        }[field]
        count = func.count(AccessGroupJoinRequest.id)
        rows = session.exec(
            _joined(select(column, count).select_from(AccessGroupJoinRequest))
            .where(*conditions).group_by(column).order_by(count.desc(), func.min(tie_break), column.asc()).limit(limit)
        ).all()
        values = [value for value, _ in rows]
        if field is JoinRequestFacetField.PROJECT:
            projects = {project.unique_id: project for project in session.exec(select(Project).where(Project.unique_id.in_(values))).all()}
            items = [
                ProjectFacetBucket(value=value, count=total, label=(projects[value].title or projects[value].project_id) if value in projects else value,
                                   project=ProjectReferenceRead.model_validate(projects[value]) if value in projects else None)
                for value, total in rows
            ]
        elif field is JoinRequestFacetField.REQUESTER:
            users = {user.unique_id: user for user in session.exec(select(User).where(User.unique_id.in_(values))).all()}

            def label(user, value):
                name = " ".join(part.strip() for part in (user.first_name, user.last_name) if part and part.strip()) if user else ""
                return name or (user.username if user else None) or value
            items = [
                RequesterFacetBucket(value=value, count=total, label=label(users.get(value), value),
                                     requester=UserPublicRead.model_validate(users[value]) if value in users else None)
                for value, total in rows
            ]
        else:
            items = [FacetBucket(value=value, label=value, count=total) for value, total in rows]
        return FacetResponse(field=field.value, limit=limit, items=items)


@router.get("/join_requests/{request_id}", response_model=JoinRequestRead)
def get_join_request(request_id: int, auth_res: Annotated[Principal, Depends(authorize_user)]):
    '''Read one join request. Visible to its requester, to those who may review it, and to platform administrators; 404 otherwise.'''
    with Session(get_engine()) as session:
        return _read(auth_res, *_visible_row(session, auth_res, request_id))


@router.patch("/join_requests/{request_id}", response_model=JoinRequestRead,
              responses={403: {"description": "Not allowed to review, or the role is not below yours"},
                         409: {"description": "`join_request_closed`, or the project is gone or pending deletion"}})
def review_join_request(request_id: int, body: JoinRequestReview, auth_res: Annotated[Principal, Depends(authorize_user)]):
    '''Approve or reject a pending request. Requires editor or above in the project (or platform administrator). Approval adds the requester at `role` (default contributor), at most your own role.'''
    with Session(get_engine()) as session:
        session.exec(select(AccessGroupJoinRequest.id).where(AccessGroupJoinRequest.id == request_id).with_for_update()).first()
        req, project_mfid, project, requester, _, may_review = _visible_row(session, auth_res, request_id)
        if not (auth_res.is_platform_admin or may_review):
            raise _error(403, "review_not_allowed", "Only project editors and above can review join requests")
        if req.status != PENDING:
            raise _error(409, "join_request_closed", f"Join request is already {req.status}", status=req.status)
        role = None
        if body.status == "approved":
            if project is None or requester is None:
                raise _error(409, "join_request_unresolvable", "The project or the requester no longer exists")
            if session.exec(select(DeletionRequest.id).where(DeletionRequest.resource_id == project.unique_id, DeletionRequest.status.in_(["pending", "approved"]))).first():
                raise _error(409, "project_not_accepting_requests", f"Project '{project.project_id}' is pending deletion")
            role = Role.from_db((body.role or JoinRequestRole.CONTRIBUTOR).value)
            require_grant(session, auth_res, project.unique_id, role, floor=Role.EDITOR)
        req.status = body.status
        req.review_time = get_utc_datetime()
        reviewer = caller_row(session, auth_res)
        req.reviewer_id = reviewer.unique_id
        req.reviewer_user_id = reviewer.id
        req.reviewer_notes = body.reviewer_notes
        req.granted_role = role.db_value if role else None
        session.add(req)
        session.flush()
        if role is not None:
            sql_add_user_to_project(requester, project.project_id, session, role=role, actor=reviewer)
        session.commit()
        return _read(auth_res, *_visible_row(session, auth_res, request_id))


@router.post("/join_requests/{request_id}/withdraw", response_model=JoinRequestRead,
             responses={403: {"description": "Only the requester can withdraw"}, 409: {"description": "`join_request_closed`"}})
def withdraw_join_request(request_id: int, auth_res: Annotated[Principal, Depends(authorize_user)]):
    '''Withdraw your own pending join request.'''
    with Session(get_engine()) as session:
        session.exec(select(AccessGroupJoinRequest.id).where(AccessGroupJoinRequest.id == request_id).with_for_update()).first()
        req = _visible_row(session, auth_res, request_id)[0]
        if req.requester_user_id != auth_res.internal_user_id:
            raise _error(403, "withdraw_not_allowed", "Only the requester can withdraw a join request")
        if req.status != PENDING:
            raise _error(409, "join_request_closed", f"Join request is already {req.status}", status=req.status)
        req.status = JoinRequestStatus.WITHDRAWN.value
        req.review_time = get_utc_datetime()
        session.add(req)
        session.commit()
        return _read(auth_res, *_visible_row(session, auth_res, request_id))

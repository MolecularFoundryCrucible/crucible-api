from datetime import datetime, date, timedelta
from typing import Annotated, Dict, List, Optional, Union
import logging

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from sqlalchemy import func, join
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select, col

from app.api.dependencies import get_engine
from app.general_utils import is_orcid
from app.models.core_models import (
    AccessGroup, Dataset, ResourceACL, Project, Sample, User,
    UserAccessGroupLink, UserProjectLink,
)
from app.utils.auth_utils import authorize_user, find_or_add_api_access, require_admin, require_groups
from app.utils.sql_utils import add_or_update, table_obj_to_dict
from app.utils.user_utils import sql_add_user_to_access_group
from app.utils.project_utils import sql_get_projects_for_user
from app.models.pagination import PaginatedResponse
from app.models.users import AccountProfileUpdate, UserCreate, UserRead, UserUpdate
from app.models.response_schemas import ApiKeyRead, ApiKeyVerify
from app.models.core_models import ApiToken
from app.models.projects import ProjectWithLead
from app.models.access_groups import AccessGroupRead

router = APIRouter(tags=["users"])
engine = get_engine()


@router.get("/users", response_model=PaginatedResponse[UserRead])
def list_users(auth_res: Annotated[tuple, Depends(authorize_user)], request: Request,
               permissive: bool = True,
               limit: int = Query(default=100, ge=1, le=1000),
               offset: int = Query(default=0, ge=0)):
    _, ag_list = auth_res

    params_dict = dict(request.query_params)
    for k in ('permissive', 'limit', 'offset'):
        params_dict.pop(k, None)

    search_cmd = select(User)
    if permissive:
        for k, v in params_dict.items():
            if hasattr(User, k):
                search_cmd = search_cmd.where(getattr(User, k).ilike(f"%{v.lower()}%"))
    else:
        for k, v in params_dict.items():
            if hasattr(User, k):
                search_cmd = search_cmd.where(getattr(User, k) == v)

    # Admins see all users; non-admins are scoped to users who share an access
    # group with them (each user has a personal access group, so this includes
    # themselves).
    if not any(ag.group_name == 'admin' for ag in ag_list):
        ag_ids = [ag.id for ag in ag_list]
        search_cmd = search_cmd.where(
            User.id.in_(
                select(UserAccessGroupLink.user_id).where(
                    UserAccessGroupLink.accessgroup_id.in_(ag_ids)
                )
            )
        )

    with Session(engine) as session:
        total = session.exec(select(func.count()).select_from(search_cmd.subquery())).one()
        items = session.exec(
            search_cmd.order_by(User.last_name, User.first_name).offset(offset).limit(limit)
        ).all()
        return PaginatedResponse(total=total, limit=limit, offset=offset, items=items)


@router.post("/users/resolve", response_model=Dict[str, UserRead])
def resolve_users(
    auth_res: Annotated[tuple, Depends(authorize_user)],
    orcids: Optional[List[str]] = Body(default=None),
    usernames: Optional[List[str]] = Body(default=None),
    emails: Optional[List[str]] = Body(default=None),
):
    '''
    Batch-resolve users by any mix of ORCIDs, usernames, or emails.
    Returns a dict keyed by ORCID: {orcid: UserRead}.
    '''
    _, ag_list = auth_res
    require_admin(ag_list)

    if not orcids and not usernames and not emails:
        return {}

    with Session(engine) as session:
        results: Dict[str, UserRead] = {}

        if orcids:
            for user in session.exec(select(User).where(User.unique_id.in_(orcids))).all():
                results[user.unique_id] = user

        if usernames:
            for user in session.exec(select(User).where(User.username.in_(usernames))).all():
                if user.unique_id not in results:
                    results[user.unique_id] = user

        if emails:
            for user in session.exec(select(User).where(User.email.in_(emails))).all():
                if user.unique_id not in results:
                    results[user.unique_id] = user

        return results


@router.get("/users/me", response_model=UserRead, deprecated=True, include_in_schema=True)
def get_current_user(auth_res: Annotated[tuple, Depends(authorize_user)]):
    '''Deprecated: use GET /account/profile.'''
    user_unique_id, _ = auth_res
    with Session(engine) as session:
        user = session.exec(select(User).where(User.unique_id == user_unique_id)).one_or_none()
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        return user


@router.patch("/users/me", response_model=UserRead, deprecated=True, include_in_schema=True)
def update_current_user(auth_res: Annotated[tuple, Depends(authorize_user)], user_update: AccountProfileUpdate):
    '''Deprecated: use PATCH /account/profile.'''
    user_unique_id, _ = auth_res
    with Session(engine) as session:
        user = session.exec(select(User).where(User.unique_id == user_unique_id)).one_or_none()
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")

        update_data = user_update.model_dump(exclude_unset=True)
        for key, value in update_data.items():
            setattr(user, key, value)

        session.add(user)
        session.commit()
        session.refresh(user)
        return user


@router.get("/users/by-username/{username}", response_model=UserRead)
def get_user_by_username(username: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    '''Look up a user by username. Admin or the matched user themselves.'''
    user_unique_id, ag_list = auth_res

    with Session(engine) as session:
        user = session.exec(select(User).where(User.username == username)).one_or_none()
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")

        if user_unique_id != user.unique_id:
            require_admin(ag_list)

        return user


@router.get("/users/{orcid}", response_model=Union[None, UserRead])
def read_user(orcid: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    '''
    Returns the User object associated with the provided ORCID if found. Otherwise returns None.
    '''
    user_unique_id, ag_list = auth_res
    if user_unique_id != orcid:
        require_admin(ag_list)

    with Session(engine) as session:
        users = session.exec(select(User).where(User.unique_id == orcid)).one_or_none()
        return users


@router.get("/users/{orcid}/apikey", response_model=ApiKeyRead)
def get_user_apikey(orcid: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    '''Returns the current API key for the given user. Admin only. Returns 404 if no key exists.'''
    _, ag_list = auth_res
    require_admin(ag_list)

    with Session(engine) as session:
        user = session.exec(select(User).where(User.unique_id == orcid)).one_or_none()
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")

        token = session.exec(
            select(ApiToken).where(ApiToken.user_unique_id == orcid)
            .order_by(ApiToken.token_created.desc())
        ).first()
        if token is None:
            raise HTTPException(status_code=404, detail="No API key found for this user")

        return ApiKeyRead(api_key=token.token)


@router.get("/users/{orcid}/apikey/verify", response_model=ApiKeyVerify)
def verify_user_apikey(orcid: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    '''Returns validity and expiry info for the given user's API key. Admin only.'''
    _, ag_list = auth_res
    require_admin(ag_list)

    with Session(engine) as session:
        user = session.exec(select(User).where(User.unique_id == orcid)).one_or_none()
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")

        one_year_ago = date.today() - timedelta(days=365)
        token = session.exec(
            select(ApiToken).where(ApiToken.user_unique_id == orcid)
            .order_by(ApiToken.token_created.desc())
        ).first()
        if token is None:
            raise HTTPException(status_code=404, detail="No API key found for this user")

        created_at = token.token_created
        if isinstance(created_at, str):
            created_dt = datetime.strptime(created_at, "%Y-%m-%d")
        else:
            created_dt = created_at

        expires_dt = created_dt + timedelta(days=365)
        valid = created_dt.date() >= one_year_ago
        return ApiKeyVerify(
            valid=valid,
            created_at=created_dt.date().isoformat(),
            expires_at=expires_dt.date().isoformat(),
        )


@router.post("/users", response_model=UserRead)
def create_user(auth_res: Annotated[tuple, Depends(authorize_user)], user_info: UserCreate, project_ids: List[str] = Body(...)):
    _, ag_list = auth_res
    require_admin(ag_list)

    # Validate required fields
    missing_fields = []
    if not user_info.first_name or not user_info.first_name.strip():
        missing_fields.append("first_name")
    if not user_info.last_name or not user_info.last_name.strip():
        missing_fields.append("last_name")
    if not user_info.unique_id or not user_info.unique_id.strip():
        missing_fields.append("unique_id")

    if missing_fields:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required user information: {', '.join(missing_fields)}. Please provide all required fields: first_name, last_name, and unique_id."
        )

    with Session(engine) as session:
        # add user to user table
        user_dict = table_obj_to_dict(user_info)
        if user_dict.get('email', None) is not None:
            user_dict['email'] = user_dict['email'].lower().strip()
            
        user = add_or_update(session, User, User.unique_id == user_info.unique_id, **user_dict)

        sql_add_user_to_access_group(user.unique_id, user.unique_id, session)

        # for each project that the user is a USER on:
        # 1. get the project object from the database
        # 2. add the project to the user object
        # 3. add an access group for the project, named by the project and add the user to the access group
        for pname in project_ids:
            props = session.exec(select(Project).where(Project.project_id == pname)).all()
            if len(props) != 1:
                logger.warning(f"Projects found for {pname}: {props} — skipping. Please ensure a project exists with a unique name.")
                continue

            prop = props[-1]

            # add project to user
            if prop not in user.projects:
                user.projects.append(prop)

            # add user to access group
            sql_add_user_to_access_group(user.unique_id, pname, session)

        session.add(user)
        session.commit()
        session.refresh(user)

    # Mirror the canonical /auth/apikey flow: provisioning a user also mints
    # their API key (idempotent) so they can authenticate immediately.
    find_or_add_api_access(user_info.unique_id, engine, regenerate=False)
    return user


@router.patch("/users/{orcid}", response_model=UserRead)
def update_user(orcid: str, auth_res: Annotated[tuple, Depends(authorize_user)], user_update: UserUpdate):
    '''Partially update a user record. Only fields provided in the request body are modified.'''
    _, ag_list = auth_res
    require_groups(ag_list, {'admin', orcid})
    with Session(engine) as session:
        user = session.exec(select(User).where(User.unique_id == orcid)).one_or_none()

        if user is None:
            raise HTTPException(status_code=404, detail="User not found")

        update_data = user_update.model_dump(exclude_unset=True)
        for key, value in update_data.items():
            setattr(user, key, value)

        session.add(user)
        session.commit()
        session.refresh(user)
        return user


@router.get("/users/{orcid}/datasets", response_model=List[str])
def read_user_datasets(orcid: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    '''
    Returns a list of all the datasets the user has access to based on the ORCID provided.
    '''
    _, ag_list = auth_res
    require_groups(ag_list, {'admin', orcid})
    with Session(engine) as session:
        # Verify user exists
        user = session.exec(select(User).where(User.unique_id == orcid)).one_or_none()

        if user is None:
            raise HTTPException(status_code=404, detail="User not found")

        ag = session.exec(select(UserAccessGroupLink).select_from(join(UserAccessGroupLink, User, User.id == UserAccessGroupLink.user_id)).where(User.unique_id == orcid)).all()
        ag_ids = [x.accessgroup_id for x in ag]
        ds_list = session.exec(
            select(Dataset)
            .select_from(join(ResourceACL, Dataset, ResourceACL.mfid == Dataset.unique_id))
            .where(ResourceACL.accessgroup_id.in_(ag_ids))
        ).all()

        return [ds.unique_id for ds in ds_list]


@router.get("/users/{orcid}/samples", response_model=List[str])
def read_user_samples(orcid: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    '''Returns a list of sample MFIDs the user has access to via their access groups.'''
    _, ag_list = auth_res
    require_groups(ag_list, {'admin', orcid})
    with Session(engine) as session:
        user = session.exec(select(User).where(User.unique_id == orcid)).one_or_none()
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")

        ag = session.exec(
            select(UserAccessGroupLink)
            .select_from(join(UserAccessGroupLink, User, User.id == UserAccessGroupLink.user_id))
            .where(User.unique_id == orcid)
        ).all()
        ag_ids = [x.accessgroup_id for x in ag]
        samples = session.exec(
            select(Sample)
            .select_from(join(ResourceACL, Sample, ResourceACL.mfid == Sample.unique_id))
            .where(ResourceACL.accessgroup_id.in_(ag_ids))
        ).all()
        return [s.unique_id for s in samples]


@router.get("/users/{orcid}/datasets/{dsid}", response_model=Dict)
def read_user_access_to_dataset(dsid: str, orcid: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    '''
    Returns boolean: True is user with provided ORCID has access to the provided dataset ID, False if no access or dataset does not exist.
    '''
    _, ag_list = auth_res
    require_groups(ag_list, {'admin', orcid})
    with Session(engine) as session:
        ag = session.exec(select(UserAccessGroupLink).select_from(join(UserAccessGroupLink, User, User.id == UserAccessGroupLink.user_id)).where(User.unique_id == orcid)).all()
        ag_ids = [x.accessgroup_id for x in ag]
        ds_acl = session.exec(
            select(ResourceACL)
            .join(Dataset, Dataset.unique_id == ResourceACL.mfid)
            .where(Dataset.unique_id == dsid)
            .filter(ResourceACL.accessgroup_id.in_(ag_ids))
        ).all()
        perms = {"read": False, "write": False}
        for acc in ds_acl:
            if acc.read is True:
                perms['read'] = True
            if acc.write is True:
                perms['write'] = True
        return perms


@router.post("/users/{orcid}/access_groups/{group_name}", response_model=AccessGroupRead)
def add_user_to_access_group(auth_res: Annotated[tuple, Depends(authorize_user)], orcid: str, group_name: str):
    _, ag_list = auth_res
    require_admin(ag_list)
    with Session(engine) as session:
        ag = sql_add_user_to_access_group(orcid, group_name, session)
        session.commit()
        session.refresh(ag)
    return ag


@router.get("/users/{orcid}/access_groups", response_model=List[str])
def list_user_access_groups(auth_res: Annotated[tuple, Depends(authorize_user)], orcid: str):
    _, ag_list = auth_res
    require_groups(ag_list, {'admin', orcid})
    with Session(engine) as session:
        user = session.exec(select(User).where(User.unique_id == orcid)).one_or_none()

        if user is None:
            raise HTTPException(status_code=404, detail="User not found")

        user_ags = session.exec(
            select(AccessGroup)
            .join(UserAccessGroupLink, UserAccessGroupLink.accessgroup_id == AccessGroup.id)
            .where(UserAccessGroupLink.user_id == user.id)
        ).all()
        return([ag.group_name for ag in user_ags])


@router.delete("/users/{orcid}/access_groups/{group_name}")
def remove_user_from_access_group(auth_res: Annotated[tuple, Depends(authorize_user)], orcid: str, group_name: str):
    '''Remove a user from an access group.'''
    _, ag_list = auth_res
    require_admin(ag_list)
    with Session(engine) as session:
        user = session.exec(select(User).where(User.unique_id == orcid)).one_or_none()
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")

        ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == group_name)).one_or_none()
        if ag is None:
            raise HTTPException(status_code=404, detail="Access group not found")

        link = session.exec(
            select(UserAccessGroupLink).where(
                UserAccessGroupLink.user_id == user.id,
                UserAccessGroupLink.accessgroup_id == ag.id
            )
        ).one_or_none()

        if link is None:
            raise HTTPException(status_code=404, detail="User is not a member of this access group")

        session.delete(link)
        session.commit()
        return {"detail": f"User {orcid} removed from access group {group_name}"}


@router.get("/users/{orcid}/projects", response_model=PaginatedResponse[ProjectWithLead])
def read_projects_for_user(orcid: str, auth_res: Annotated[tuple, Depends(authorize_user)],
                           limit: int = Query(default=100, ge=1, le=1000),
                           offset: int = Query(default=0, ge=0)):
    '''
    Returns a paginated list of the projects that this User is associated with, including the resolved project lead.
    '''
    _, ag_list = auth_res
    require_groups(ag_list, {'admin', orcid})

    with Session(engine) as session:
        user = session.exec(select(User).where(User.unique_id == orcid)).one_or_none()
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")

        base = (
            select(Project)
            .join(UserProjectLink, Project.project_id == UserProjectLink.project_id)
            .where(UserProjectLink.user_id == user.id)
        )
        total = session.exec(select(func.count()).select_from(base.subquery())).one()
        projects = session.exec(
            base.options(selectinload(Project.resource))
            .order_by(col(Project.project_id))
            .offset(offset).limit(limit)
        ).all()

        orcids = [p.project_lead_orcid for p in projects if p.project_lead_orcid]
        leads = {u.unique_id: u for u in session.exec(select(User).where(User.unique_id.in_(orcids))).all()}
        items = [
            ProjectWithLead(
                **proj.model_dump(),
                lead=leads.get(proj.project_lead_orcid),
                creation_time=proj.resource.creation_time if proj.resource else None,
                modification_time=proj.resource.modification_time if proj.resource else None,
            )
            for proj in projects
        ]
        return PaginatedResponse(total=total, limit=limit, offset=offset, items=items)

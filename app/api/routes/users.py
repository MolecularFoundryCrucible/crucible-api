from datetime import datetime, date, timedelta
from typing import Annotated, Dict, List, Optional, Union
import logging

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from sqlalchemy import and_, func, join, literal, text
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select, col

from app.api.dependencies import get_engine, get_trgm_search_threshold
from app.general_utils import is_orcid, is_valid_mfid, get_secret
from app.models.core_models import (
    AccessGroup, Dataset, Project, Sample, User,
    UserAccessGroupLink,
)
from app.utils.auth_utils import authorize_user, fetch_acl_rows, find_or_add_api_access, has_platform_admin_access, is_instrument_group_member, permission_filter, require_platform_admin, require_self_or_platform_admin, resolve_access_principal
from app.utils.permissions import AccessGroupType, effective_role, Principal, Role
from app.utils.sql_utils import add_or_update, deletion_filter_for, table_obj_to_dict
from app.utils.user_utils import assert_username_available, resolve_user_by_identifier, sql_add_user_to_access_group
from app.utils.project_utils import sql_get_projects_for_user
from app.models.pagination import PaginatedResponse
from app.models.users import AccountProfileUpdate, UserCreate, UserPublicRead, UserRead, UserUpdate
from app.models.response_schemas import ApiKeyRead, ApiKeyVerify
from app.models.core_models import ApiToken
from app.models.projects import ProjectMinimalRead, ProjectWithLead
from app.models.access_groups import AccessGroupRead
from app.models.access import EffectiveAccess, EffectiveResourceAccessRead

router = APIRouter(tags=["users"])
engine = get_engine()


@router.get("/users", response_model=PaginatedResponse[UserPublicRead])
def list_users(auth_res: Annotated[Principal, Depends(authorize_user)], request: Request,
               unique_id: Optional[str] = Query(default=None, description="Exact ORCID or service-account MFID."),
               username: Optional[str] = Query(default=None, description="Exact username."),
               email: Optional[str] = Query(default=None, description="Exact email address."),
               permissive: bool = True,
               is_service_account: Optional[bool] = None,
               limit: int = Query(default=100, ge=1, le=1000),
               offset: int = Query(default=0, ge=0)):
    ag_list = auth_res.groups
    params_dict = dict(request.query_params)
    for k in ('unique_id', 'username', 'email', 'permissive', 'limit', 'offset', 'is_service_account'):
        params_dict.pop(k, None)

    search_cmd = select(User)
    exact_lookup = unique_id is not None or username is not None or email is not None
    if unique_id is not None:
        search_cmd = search_cmd.where(User.unique_id == unique_id)
    if username is not None:
        search_cmd = search_cmd.where(func.lower(User.username) == username.lower())
    if email is not None:
        search_cmd = search_cmd.where(func.lower(User.email) == email.lower())

    if permissive:
        for k, v in params_dict.items():
            if hasattr(User, k):
                search_cmd = search_cmd.where(getattr(User, k).ilike(f"%{v.lower()}%"))
    else:
        for k, v in params_dict.items():
            if hasattr(User, k):
                search_cmd = search_cmd.where(getattr(User, k) == v)

    if is_service_account is not None:
        search_cmd = search_cmd.where(User.is_service_account == is_service_account)

    # Admins see the full directory; non-admins are scoped to users who share
    # an access group with them. Collection responses are public-safe for both.
    if not exact_lookup and not has_platform_admin_access(auth_res):
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
        items = [UserPublicRead.model_validate(user) for user in items]
        return PaginatedResponse(total=total, limit=limit, offset=offset, items=items)


@router.post("/users/resolve", response_model=Dict[str, UserPublicRead])
def resolve_users(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    orcids: Optional[List[str]] = Body(default=None),
    usernames: Optional[List[str]] = Body(default=None),
    emails: Optional[List[str]] = Body(default=None),
):
    '''
    Batch-resolve users by any mix of ORCIDs, usernames, or emails.
    Returns a dict keyed by ORCID: {orcid: UserPublicRead}. No email included.
    Any authenticated user can call this. Admins needing email should use GET /users/{orcid}.
    '''
    if not orcids and not usernames and not emails:
        return {}

    with Session(engine) as session:
        results: Dict[str, UserPublicRead] = {}

        if orcids:
            for user in session.exec(select(User).where(User.unique_id.in_(orcids))).all():
                results[user.unique_id] = UserPublicRead.model_validate(user)

        if usernames:
            normalized_usernames = [u.lower() for u in usernames]
            for user in session.exec(select(User).where(User.username.in_(normalized_usernames))).all():
                if user.unique_id not in results:
                    results[user.unique_id] = UserPublicRead.model_validate(user)

        if emails:
            normalized_emails = [e.lower() for e in emails]
            for user in session.exec(select(User).where(func.lower(User.email).in_(normalized_emails))).all():
                if user.unique_id not in results:
                    results[user.unique_id] = UserPublicRead.model_validate(user)

        return results


@router.get("/users/me", response_model=UserRead, deprecated=True, include_in_schema=True)
def get_current_user(auth_res: Annotated[Principal, Depends(authorize_user)]):
    '''Deprecated: use GET /account/profile.'''
    user_unique_id = auth_res.user_id
    with Session(engine) as session:
        user = session.exec(select(User).where(User.unique_id == user_unique_id)).one_or_none()
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        return user


@router.patch("/users/me", response_model=UserRead, deprecated=True, include_in_schema=True)
def update_current_user(auth_res: Annotated[Principal, Depends(authorize_user)], user_update: AccountProfileUpdate):
    '''Deprecated: use PATCH /account/profile.'''
    user_unique_id = auth_res.user_id
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


@router.get("/users/search", response_model=List[UserPublicRead])
def search_users(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    q: str = Query(min_length=3, description="Search term matched against username, first name, and last name."),
    limit: int = Query(default=20, ge=1, le=50),
):
    '''
    Search users by username, first name, or last name. Returns public profiles only (no email).
    Minimum query length: 3 characters. Maximum results: 50.
    '''
    with Session(engine) as session:
        session.execute(text(f"SET LOCAL pg_trgm.word_similarity_threshold = {get_trgm_search_threshold()}"))
        tokens = q.lower().split()
        query = select(User)
        for token in tokens:
            query = query.where(
                User.first_name.ilike(f"{token}%") |
                User.last_name.ilike(f"{token}%") |
                User.username.ilike(f"{token}%") |
                literal(token).op("<%")(User.first_name) |
                literal(token).op("<%")(User.last_name) |
                literal(token).op("<%")(User.username)
            )
        results = session.exec(
            query.order_by(
                func.greatest(
                    func.word_similarity(q, User.first_name),
                    func.word_similarity(q, User.last_name),
                    func.coalesce(func.word_similarity(q, User.username), 0),
                ).desc()
            )
            .limit(limit)
        ).all()
    return results


@router.get("/users/by-username/{username}", response_model=UserPublicRead)
def get_user_by_username(username: str, response: Response,
                         auth_res: Annotated[Principal, Depends(authorize_user)]):
    '''Look up a user by username. Returns public profile (no email). Any authenticated user.'''
    with Session(engine) as session:
        user = session.exec(select(User).where(User.username == username.lower())).one_or_none()
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        response.headers["Content-Location"] = f"/users/{user.unique_id}"
        return UserPublicRead.model_validate(user)


@router.get("/users/{user_id}", response_model=Union[UserRead, UserPublicRead])
def read_user(user_id: str, response: Response,
              auth_res: Annotated[Principal, Depends(authorize_user)]):
    with Session(engine) as session:
        user = resolve_user_by_identifier(session, user_id)

    canonical_reference = is_orcid(user_id) or is_valid_mfid(user_id)
    if canonical_reference and auth_res.user_id != user.unique_id:
        require_platform_admin(auth_res)

    is_admin = has_platform_admin_access(auth_res)
    if not canonical_reference:
        response.headers["Content-Location"] = f"/users/{user.unique_id}"

    if auth_res.user_id == user.unique_id or is_admin:
        return UserRead.model_validate(user)
    return UserPublicRead.model_validate(user)


@router.post("/users/{user_id}/apikey", response_model=ApiKeyRead)
def get_or_provision_user_apikey(user_id: str, auth_res: Annotated[Principal, Depends(authorize_user)]):
    '''Return the user's API key, minting one if they don't have it yet. Admin only. 404 if the user doesn't exist.'''
    require_platform_admin(auth_res)

    with Session(engine) as session:
        user = session.exec(select(User).where(User.unique_id == user_id)).one_or_none()
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")

    return ApiKeyRead(api_key=find_or_add_api_access(user_id, engine, regenerate=False))


@router.get("/users/{user_id}/apikey/verify", response_model=ApiKeyVerify)
def verify_user_apikey(user_id: str, auth_res: Annotated[Principal, Depends(authorize_user)]):
    '''Returns validity and expiry info for the given user's API key. Admin only.'''
    require_platform_admin(auth_res)

    with Session(engine) as session:
        user = session.exec(select(User).where(User.unique_id == user_id)).one_or_none()
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")

        one_year_ago = date.today() - timedelta(days=365)
        token = session.exec(
            select(ApiToken).where(ApiToken.user_unique_id == user_id)
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

def find_mf_user_proposals(orcid):
    import requests

    mf_user_db_url = "https://foundry-admin.lbl.gov/api/JSON/sciCat-GetUser.aspx?"
    apikey = get_secret("PROPDB_APIKEY", "propdb-apikey/versions/1")
    request_url = f"{mf_user_db_url}key={apikey}&orcid={orcid}"
    assert apikey is not None
    print(f"{mf_user_db_url}key=****&orcid={orcid}")
    response = requests.request(method="get", url=request_url)

    found_proposals = []
    if len(response.json()) > 0 and response.status_code == 200:
        logger.info(f'{response.json()=}')
        for u in response.json():
            found_proposals += u.get('proposals', [])
        
        found_proposals = list(set(found_proposals))
        
    return found_proposals


@router.post("/users", response_model=UserRead)
def create_user(auth_res: Annotated[Principal, Depends(authorize_user)], user_info: UserCreate, project_ids: List[str] = Body(...)):
    require_platform_admin(auth_res)

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
        if user_info.username:
            assert_username_available(session, user_info.username, exclude_orcid=user_info.unique_id)
        user_dict = table_obj_to_dict(user_info)
        if user_dict.get('email', None) is not None:
            user_dict['email'] = user_dict['email'].lower().strip()
            
        user = add_or_update(session, User, User.unique_id == user_info.unique_id, **user_dict)

        sql_add_user_to_access_group(
            user.unique_id,
            user.unique_id,
            session,
            role=Role.OWNER,
            group_type=AccessGroupType.IDENTITY,
        )

        # check if the user is in the MF Proposal Database and should be added to any proposals
        user_proposal_ids = find_mf_user_proposals(user.unique_id)
        project_ids.append(user_proposal_ids)

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
            sql_add_user_to_access_group(
                user.unique_id,
                prop.unique_id,
                session,
                group_type=AccessGroupType.PROJECT,
            )

        session.add(user)
        session.commit()
        session.refresh(user)

    # Mirror the canonical /auth/apikey flow: provisioning a user also mints
    # their API key (idempotent) so they can authenticate immediately.
    find_or_add_api_access(user_info.unique_id, engine, regenerate=False)
    return user


@router.patch("/users/{user_id}", response_model=UserRead)
def update_user(user_id: str, auth_res: Annotated[Principal, Depends(authorize_user)], user_update: UserUpdate):
    '''Partially update a user record. Only fields provided in the request body are modified.'''
    require_self_or_platform_admin(auth_res, user_id)
    with Session(engine) as session:
        user = session.exec(select(User).where(User.unique_id == user_id)).one_or_none()

        if user is None:
            raise HTTPException(status_code=404, detail="User not found")

        update_data = user_update.model_dump(exclude_unset=True)
        if "username" in update_data and update_data["username"] is not None:
            assert_username_available(session, update_data["username"], exclude_orcid=user_id)
        for key, value in update_data.items():
            setattr(user, key, value)

        session.add(user)
        session.commit()
        session.refresh(user)
        return user


@router.get("/users/{user_id}/datasets", response_model=List[str], deprecated=True)
def read_user_datasets(user_id: str, auth_res: Annotated[Principal, Depends(authorize_user)]):
    '''
    Deprecated compatibility route returning dataset MFIDs. Use GET /datasets?accessible_to_user={user_id}.
    '''
    with Session(engine) as session:
        access_principal = resolve_access_principal(session, auth_res, user_id)
        return session.exec(
            select(Dataset.unique_id)
            .where(permission_filter(access_principal, Dataset))
            .where(deletion_filter_for(Dataset))
            .order_by(Dataset.unique_id.desc())
        ).all()


@router.get("/users/{user_id}/samples", response_model=List[str], deprecated=True)
def read_user_samples(user_id: str, auth_res: Annotated[Principal, Depends(authorize_user)]):
    '''Deprecated compatibility route returning sample MFIDs. Use GET /samples?accessible_to_user={user_id}.'''
    with Session(engine) as session:
        access_principal = resolve_access_principal(session, auth_res, user_id)
        return session.exec(
            select(Sample.unique_id)
            .where(permission_filter(access_principal, Sample))
            .where(deletion_filter_for(Sample))
            .order_by(Sample.unique_id.desc())
        ).all()


@router.get("/users/{user_id}/datasets/{dataset_mfid}", response_model=EffectiveResourceAccessRead)
def read_user_access_to_dataset(dataset_mfid: str, user_id: str, auth_res: Annotated[Principal, Depends(authorize_user)]):
    '''Returns the user's effective role on one dataset. Limited to self and platform administrators.'''
    with Session(engine) as session:
        access_principal = resolve_access_principal(session, auth_res, user_id)
        dataset = session.exec(
            select(Dataset).where(Dataset.unique_id == dataset_mfid)
        ).one_or_none()
        if dataset is None:
            raise HTTPException(status_code=404, detail="Dataset not found")

        if access_principal.is_platform_admin:
            access = EffectiveAccess.PLATFORM_ADMIN
        else:
            role = effective_role(access_principal, fetch_acl_rows(session, dataset_mfid))
            access = EffectiveAccess(role.db_value) if role is not None else EffectiveAccess.NONE

        return EffectiveResourceAccessRead(
            resource_mfid=dataset_mfid,
            user_id=access_principal.user_id,
            effective_access=access,
        )


@router.post(
    "/users/{user_id}/access_groups/{group_name}",
    response_model=AccessGroupRead,
    deprecated=True,
)
def add_user_to_access_group(auth_res: Annotated[Principal, Depends(authorize_user)], user_id: str, group_name: str):
    '''Add a user to an access group. Not for project groups. Use
    POST /projects/{project_ref}/users/{orcid}, which applies the project's own
    role cap instead of platform-admin-only.'''
    require_platform_admin(auth_res)
    with Session(engine) as session:
        user = session.exec(select(User).where(User.unique_id == user_id)).one_or_none()
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == group_name)).one_or_none()
        if ag is None:
            raise HTTPException(status_code=404, detail="Access group not found")
        if ag.group_type == AccessGroupType.IDENTITY.value:
            raise HTTPException(status_code=409, detail="Identity-group membership cannot be changed")
        if ag.group_type == AccessGroupType.SYSTEM.value:
            raise HTTPException(status_code=409, detail="System-group membership cannot be changed")
        if ag.group_type == AccessGroupType.PROJECT.value:
            raise HTTPException(
                status_code=400,
                detail=f"'{group_name}' is a project. Use POST /projects/{group_name}/users/{user_id} instead.")
        ag = sql_add_user_to_access_group(user_id, group_name, session)
        session.commit()
        session.refresh(ag)
    return ag


@router.get("/users/{user_id}/access_groups", response_model=List[str])
def list_user_access_groups(auth_res: Annotated[Principal, Depends(authorize_user)], user_id: str):
    require_self_or_platform_admin(auth_res, user_id)
    with Session(engine) as session:
        user = session.exec(select(User).where(User.unique_id == user_id)).one_or_none()

        if user is None:
            raise HTTPException(status_code=404, detail="User not found")

        user_ags = session.exec(
            select(AccessGroup)
            .join(UserAccessGroupLink, UserAccessGroupLink.accessgroup_id == AccessGroup.id)
            .where(UserAccessGroupLink.user_id == user.id)
        ).all()
        return([ag.group_name for ag in user_ags])


@router.delete(
    "/users/{user_id}/access_groups/{group_name}",
    deprecated=True,
)
def remove_user_from_access_group(auth_res: Annotated[Principal, Depends(authorize_user)], user_id: str, group_name: str):
    '''Remove a user from an access group. Not for project groups. Use
    DELETE /projects/{project_ref}/users/{orcid}, which keeps membership records
    in step instead of only revoking access-group standing.'''
    require_platform_admin(auth_res)
    with Session(engine) as session:
        ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == group_name)).one_or_none()
        if ag is None:
            raise HTTPException(status_code=404, detail="Access group not found")
        if ag.group_type == AccessGroupType.IDENTITY.value:
            raise HTTPException(status_code=409, detail="Identity-group membership cannot be changed")
        if ag.group_type == AccessGroupType.SYSTEM.value:
            raise HTTPException(status_code=409, detail="System-group membership cannot be changed")
        if ag.group_type == AccessGroupType.PROJECT.value:
            raise HTTPException(
                status_code=400,
                detail=f"'{group_name}' is a project. Use DELETE /projects/{group_name}/users/{user_id} instead.")

        user = session.exec(select(User).where(User.unique_id == user_id)).one_or_none()
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")

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
        return {"detail": f"User {user_id} removed from access group {group_name}"}


@router.get("/users/{user_id}/projects", response_model=PaginatedResponse[Union[ProjectWithLead, ProjectMinimalRead]])
def read_projects_for_user(user_id: str, auth_res: Annotated[Principal, Depends(authorize_user)],
                           limit: int = Query(default=100, ge=1, le=1000),
                           offset: int = Query(default=0, ge=0)):
    '''
    Returns a paginated list of the projects that this User is associated with.
    Self or a platform admin gets full details including the resolved lead.
    A caller with standing in any instrument group gets project_id and title
    only, for rendering a project picker — nothing wider.
    '''
    with Session(engine) as session:
        minimal = False
        if auth_res.user_id != user_id and not auth_res.is_platform_admin:
            if not is_instrument_group_member(auth_res, session):
                raise HTTPException(status_code=403, detail="Not authorized")
            minimal = True

        user = session.exec(select(User).where(User.unique_id == user_id)).one_or_none()
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")

        base = (
            select(Project)
            .join(AccessGroup, AccessGroup.group_name == Project.unique_id)
            .join(UserAccessGroupLink, and_(
                UserAccessGroupLink.accessgroup_id == AccessGroup.id,
                UserAccessGroupLink.user_id == user.id,
            ))
        )
        total = session.exec(select(func.count()).select_from(base.subquery())).one()
        projects = session.exec(
            base.options(selectinload(Project.resource))
            .order_by(col(Project.project_id))
            .offset(offset).limit(limit)
        ).all()

        if minimal:
            items = [
                ProjectMinimalRead(unique_id=p.unique_id, project_id=p.project_id, title=p.title)
                for p in projects
            ]
            return PaginatedResponse(total=total, limit=limit, offset=offset, items=items)

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

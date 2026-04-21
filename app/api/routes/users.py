from typing import Annotated, Dict, List, Optional, Union

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from sqlalchemy import join
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.general_utils import is_orcid
from app.models.core_models import (
    AccessGroup, Dataset, ResourceACL, Project, User,
    UserAccessGroupLink, UserProjectLink,
)
from app.utils.auth_utils import authorize_user, require_groups
from app.utils.sql_utils import add_or_update, table_obj_to_dict
from app.utils.user_utils import sql_add_user_to_access_group
from app.utils.project_utils import sql_get_projects_for_user
from app.models.users import UserCreate, UserRead, UserUpdate
from app.models.projects import ProjectWithLead
from app.models.access_groups import AccessGroupRead

router = APIRouter(tags=["users"])
engine = get_engine()


@router.get("/users", response_model=List[UserRead])
def list_users(auth_res: Annotated[tuple, Depends(authorize_user)], request: Request, permissive=True):
    _, ag_list = auth_res
    require_groups(ag_list)

    query_params = request.query_params
    params_dict = dict(query_params)
    search_cmd = select(User)

    if permissive:
        for k, v in params_dict.items():
            if hasattr(User, k):
                search_cmd = search_cmd.where(getattr(User, k).ilike(f"%{v.lower()}%"))
    else:
        for k, v in params_dict.items():
            if hasattr(User, k):
                search_cmd = search_cmd.where(getattr(User, k) == v)

    with Session(engine) as session:
        users = session.exec(search_cmd).all()
        return users


@router.get("/users/{orcid}", response_model=Union[None, UserRead])
def read_user(orcid: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    '''
    Returns the User object associated with the provided ORCID if found. Otherwise returns None.
    '''
    user_unique_id, ag_list = auth_res

    # you can get yourself
    if user_unique_id != orcid:
        require_groups(ag_list)

    with Session(engine) as session:
        users = session.exec(select(User).where(User.unique_id == orcid)).one_or_none()
        return users


@router.post("/users", response_model=UserRead)
def create_user(auth_res: Annotated[tuple, Depends(authorize_user)], user_info: UserCreate, project_ids: List[str] = Body(...)):
    _, ag_list = auth_res
    require_groups(ag_list)

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
        user = add_or_update(session, User, User.unique_id == user_info.unique_id, **user_dict)
        session.commit()

        sql_add_user_to_access_group(user.unique_id, user.unique_id, session)

        # for each project that the user is a USER on:
        # 1. get the project object from the database
        # 2. add the project to the user object
        # 3. add an access group for the project, named by the project and add the user to the access group
        for pname in project_ids:
            props = session.exec(select(Project).where(Project.project_id == pname)).all()
            if len(props) != 1:
                print(f"Projects found for {pname}: {props} --- skipping.  Please ensure a project exists with a unique name.")
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
    return user


@router.patch("/users/{orcid}", response_model=UserRead)
def update_user(orcid: str, auth_res: Annotated[tuple, Depends(authorize_user)], user_update: UserUpdate):
    _, ag_list = auth_res
    require_groups(ag_list, {'admin', orcid})
    '''
    Partially update a user record. Only fields provided in the request body are modified.
    '''
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
    require_groups(ag_list)
    with Session(engine) as session:
        ag = sql_add_user_to_access_group(orcid, group_name, session)
    return(ag)


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
    '''
    Remove a user from an access group.
    '''
    _, ag_list = auth_res
    require_groups(ag_list)
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


@router.get("/users/{orcid}/projects", response_model=List[ProjectWithLead])
def read_projects_for_user(orcid: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    '''
    Returns a list of the projects that this User is associated with, including the resolved project lead.
    '''
    _, ag_list = auth_res
    require_groups(ag_list, {'admin', orcid})

    with Session(engine) as session:
        projects = sql_get_projects_for_user(orcid, session)

        if projects is None:
            raise HTTPException(status_code=404, detail="User not found")

        orcids = [p.project_lead_orcid for p in projects if p.project_lead_orcid]
        leads = {u.unique_id: u for u in session.exec(select(User).where(User.unique_id.in_(orcids))).all()}
        return [
            ProjectWithLead(**proj.model_dump(), lead=leads.get(proj.project_lead_orcid))
            for proj in projects
        ]

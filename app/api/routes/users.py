from typing import Annotated, Dict, List, Optional, Union

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from sqlalchemy import join
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.general_utils import is_orcid
from app.models.core_models import (
    AccessGroup, Dataset, DatasetACL, Project, User,
    UserAccessGroupLink, UserProjectLink,
)
from app.utils.auth_utils import authorize_user, is_admin
from app.utils.sql_utils import add_or_update, table_obj_to_dict
from app.utils.user_utils import sql_add_user_to_access_group
from app.utils.project_utils import sql_get_projects_for_user
from app.models.users import UserRead, UserUpdate

router = APIRouter(tags=["users"])
engine = get_engine()


@router.get("/users", response_model=List[User])
def list_users(admin_acct: Annotated[str, Depends(is_admin)], request: Request, permissive=True):

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


@router.get("/users/{orcid}", response_model=Union[None, User])
def read_user(orcid: str, admin_acct: Annotated[str, Depends(is_admin)]):
    '''
    Returns the User object associated with the provided ORCID if found. Otherwise returns None.
    '''
    with Session(engine) as session:
        users = session.exec(select(User).where(User.orcid == orcid)).one_or_none()
        return users


@router.post("/users", response_model=User)
def create_user(admin_acct: Annotated[str, Depends(is_admin)], user_info: User, project_ids: List[str] = Body(...)):
    """
    Add or update a user record in the database.
    Add the user to an Access Group with their ORCID
    Add the user to Access Groups for any project they are a USER on
    Create a link to the projects that the User is a USER on

    Args:
        admin_acct: Admin account identifier
        user_info: Instance of class User
        project_ids: List of project IDs the user belongs to

    Returns:
        Added or updated user object

    Raises:
        HTTPException: If required user fields are missing
    """
    # Validate required fields
    missing_fields = []
    if not user_info.first_name or not user_info.first_name.strip():
        missing_fields.append("first_name")
    if not user_info.last_name or not user_info.last_name.strip():
        missing_fields.append("last_name")
    if not user_info.orcid or not user_info.orcid.strip():
        missing_fields.append("orcid")

    if missing_fields:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required user information: {', '.join(missing_fields)}. Please provide all required fields: first_name, last_name, and orcid."
        )

    with Session(engine) as session:
        # add user to user table
        user_dict = table_obj_to_dict(user_info)
        user = add_or_update(session, User, User.orcid == user_info.orcid, **user_dict)
        session.commit()

        sql_add_user_to_access_group(user.orcid, user.orcid, session)

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
            sql_add_user_to_access_group(user.orcid, pname, session)

        session.add(user)
        session.commit()
    return user_info


@router.patch("/users/{orcid}", response_model=UserRead)
def update_user(orcid: str, admin_acct: Annotated[str, Depends(is_admin)], user_update: UserUpdate):
    '''
    Partially update a user record. Only fields provided in the request body are modified.
    '''
    with Session(engine) as session:
        user = session.exec(select(User).where(User.orcid == orcid)).one_or_none()

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
def read_user_datasets(orcid: str, admin_acct: Annotated[str, Depends(is_admin)]):
    '''
    Returns a list of all the datasets the user has access to based on the ORCID provided.
    '''
    with Session(engine) as session:
        # Verify user exists
        user = session.exec(select(User).where(User.orcid == orcid)).one_or_none()

        if user is None:
            raise HTTPException(status_code=404, detail="User not found")

        ag = session.exec(select(UserAccessGroupLink).select_from(join(UserAccessGroupLink, User, User.id == UserAccessGroupLink.user_id)).where(User.orcid == orcid)).all()
        ag_ids = [x.accessgroup_id for x in ag]
        ds_list = session.exec(select(Dataset).select_from(join(DatasetACL, Dataset, DatasetACL.dataset_id == Dataset.id)).where(DatasetACL.accessgroup_id.in_(ag_ids))).all()

        return [ds.unique_id for ds in ds_list]


@router.get("/users/{orcid}/datasets/{dsid}", response_model=Dict)
def read_user_access_to_dataset(dsid: str, orcid: str, admin_acct: Annotated[str, Depends(is_admin)]):
    '''
    Returns boolean: True is user with provided ORCID has access to the provided dataset ID, False if no access or dataset does not exist.
    '''
    with Session(engine) as session:
        ag = session.exec(select(UserAccessGroupLink).select_from(join(UserAccessGroupLink, User, User.id == UserAccessGroupLink.user_id)).where(User.orcid == orcid)).all()
        ag_ids = [x.accessgroup_id for x in ag]
        ds_acl = session.exec(select(DatasetACL).select_from(join(DatasetACL, Dataset, DatasetACL.dataset_id == Dataset.id)).where(Dataset.unique_id == dsid).filter(DatasetACL.accessgroup_id.in_(ag_ids))).all()
        perms = {"read": False, "write": False}
        for acc in ds_acl:
            if acc.read is True:
                perms['read'] = True
            if acc.write is True:
                perms['write'] = True
        return(perms)


@router.post("/users/{orcid}/access_groups/{group_name}", response_model=AccessGroup)
def add_user_to_access_group(admin_acct: Annotated[str, Depends(is_admin)], orcid: str, group_name: str):
    with Session(engine) as session:
        ag = sql_add_user_to_access_group(orcid, group_name, session)
    return(ag)


@router.get("/users/{orcid}/access_groups", response_model=List[str])
def list_user_access_groups(auth_res: Annotated[tuple, Depends(authorize_user)], orcid: str):
    access_group_name, ag_ids = auth_res
    with Session(engine) as session:
        # Verify user exists
        user = session.exec(select(User).where(User.orcid == orcid)).one_or_none()

        if user is None:
            raise HTTPException(status_code=404, detail="User not found")

        ags = session.exec(select(AccessGroup).where(AccessGroup.id.in_(ag_ids))).all()
        return([ag.group_name for ag in ags])


@router.delete("/users/{orcid}/access_groups/{group_name}")
def remove_user_from_access_group(admin_acct: Annotated[str, Depends(is_admin)], orcid: str, group_name: str):
    '''
    Remove a user from an access group.
    '''
    with Session(engine) as session:
        user = session.exec(select(User).where(User.orcid == orcid)).one_or_none()
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


@router.get("/users/{orcid}/projects", response_model=List[Project])
def read_projects_for_user(orcid: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    '''
    Returns a list of the projects that this User is associated with.
    '''
    access_group_name, ag_ids = auth_res

    if not access_group_name.startswith('admin') and access_group_name != orcid:
        raise HTTPException(status_code=403, detail="Not authorized to view projects for this user")

    with Session(engine) as session:
        projects = sql_get_projects_for_user(orcid, session)

        if projects is None:
            raise HTTPException(status_code=404, detail="User not found")

        return projects

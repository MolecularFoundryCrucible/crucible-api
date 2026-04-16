from typing import Annotated, List, Union

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, join
from sqlalchemy.orm import joinedload
from sqlmodel import and_, or_, Session, select
import networkx as nx
from networkx.readwrite import json_graph

from app.api.dependencies import get_engine
from app.general_utils import is_orcid
from app.models.core_models import (
    AccessGroup, Project, Sample, User, UserProjectLink, UserAccessGroupLink,
    DatasetACL, Dataset, DatasetSampleLink
)
from app.utils.auth_utils import authorize_user, require_groups
from app.utils.sql_utils import add_or_update
from app.utils.user_utils import sql_add_user_to_access_group
from app.utils.project_utils import sql_get_projects_for_user
from app.models.projects import ProjectCreate, ProjectRead, ProjectUpdate, ProjectWithLead
from app.models.users import UserRead

router = APIRouter(tags=["projects"])
engine = get_engine()


@router.post("/projects", response_model=ProjectRead)
def create_project(auth_res: Annotated[tuple, Depends(authorize_user)], P: ProjectCreate):
    """
    Create a new project. Either project_lead_orcid or project_lead_email must be
    provided and must correspond to an existing user. If email is given, lbl_email
    is checked first, then email. The resolved ORCID is stored on the project.
    Returns 409 if a project with the given project_id already exists.
    """
    if not P.project_lead_orcid and not P.project_lead_email:
        raise HTTPException(status_code=400, detail="Either project_lead_orcid or project_lead_email must be provided.")

    with Session(engine) as session:
        existing = session.exec(select(Project).where(Project.project_id == P.project_id)).one_or_none()
        if existing is not None:
            raise HTTPException(status_code=409, detail=f"Project '{P.project_id}' already exists. Use PATCH to update it.")

        if P.project_lead_orcid:
            project_lead = session.exec(select(User).where(User.unique_id == P.project_lead_orcid)).one_or_none()
            identifier = P.project_lead_orcid
        else:
            email = P.project_lead_email.lower()
            project_lead = session.exec(
                select(User).where(
                    (func.lower(User.lbl_email) == email) | (func.lower(User.email) == email)
                )
            ).first()
            identifier = P.project_lead_email

        if project_lead is None:
            raise HTTPException(status_code=404, detail=f"Project lead user '{identifier}' not found")

        prop = Project(
            project_id=P.project_id,
            organization=P.organization,
            status=P.status,
            title=P.title,
            project_lead_orcid=project_lead.unique_id,
            project_lead_email=project_lead.lbl_email or project_lead.email,
        )
        session.add(prop)

        ag = add_or_update(session,
                           AccessGroup,
                           AccessGroup.group_name == P.project_id,
                           group_name=P.project_id)

        if project_lead not in ag.users:
            ag.users.append(project_lead)
        if prop not in project_lead.projects:
            project_lead.projects.append(prop)

        session.commit()
        session.refresh(prop)
        return prop


@router.get("/projects", response_model=List[ProjectWithLead])
def list_projects(auth_res: Annotated[tuple, Depends(authorize_user)]):
    '''
    Returns a list of projects the user is authorized to see, including the resolved project lead.
    '''
    user_unique_id, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        if user_unique_id.startswith('admin'):
            props = session.exec(select(Project)).all()
        elif is_orcid(user_unique_id):
            props = sql_get_projects_for_user(user_unique_id, session) or []
        else:
            raise HTTPException(status_code=403, detail="Not authorized to view projects")

        orcids = [p.project_lead_orcid for p in props if p.project_lead_orcid]
        leads = {u.unique_id: u for u in session.exec(select(User).where(User.unique_id.in_(orcids))).all()}
        return [
            ProjectWithLead(**proj.model_dump(), lead=leads.get(proj.project_lead_orcid))
            for proj in props
        ]


@router.get("/projects/{proj_id}", response_model=Union[None, ProjectWithLead])
def read_project(proj_id: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    '''
    Returns the project object for the provided ID, including the resolved project lead.
    '''
    with Session(engine) as session:
        prop = session.exec(select(Project).where(Project.project_id == proj_id)).one_or_none()
        if prop is None:
            return None
        lead = session.exec(select(User).where(User.unique_id == prop.project_lead_orcid)).one_or_none() if prop.project_lead_orcid else None
        return ProjectWithLead(**prop.model_dump(), lead=lead)


@router.patch("/projects/{proj_id}", response_model=ProjectWithLead)
def update_project(proj_id: str, auth_res: Annotated[tuple, Depends(authorize_user)], project_update: ProjectUpdate):
    '''
    Partially update a project record. Only fields provided in the request body are modified.
    Accessible by admins and the project lead.
    If project_lead_orcid or project_lead_email is provided, it must correspond to an existing user.
    '''
    user_unique_id, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        project = session.exec(select(Project).where(Project.project_id == proj_id)).one_or_none()

        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")

        if not user_unique_id.startswith('admin'):
            if is_orcid(user_unique_id):
                requester = session.exec(select(User).where(User.unique_id == user_unique_id)).one_or_none()
                if not requester or requester.unique_id != project.project_lead_orcid:
                    raise HTTPException(status_code=403, detail="Only the project lead or an admin can update a project")
            else:
                raise HTTPException(status_code=403, detail="Only the project lead or an admin can update a project")

        update_data = project_update.model_dump(exclude_unset=True)

        if "project_lead_orcid" in update_data:
            lead = session.exec(select(User).where(User.unique_id == update_data["project_lead_orcid"])).one_or_none()
            if lead is None:
                raise HTTPException(status_code=404, detail=f"Project lead user '{update_data['project_lead_orcid']}' not found")
            update_data["project_lead_email"] = lead.lbl_email or lead.email
        elif "project_lead_email" in update_data:
            email = update_data.pop("project_lead_email").lower()
            lead = session.exec(
                select(User).where(
                    (func.lower(User.lbl_email) == email) | (func.lower(User.email) == email)
                )
            ).first()
            if lead is None:
                raise HTTPException(status_code=404, detail=f"Project lead user '{email}' not found")
            update_data["project_lead_orcid"] = lead.unique_id
            update_data["project_lead_email"] = lead.lbl_email or lead.email
        else:
            update_data.pop("project_lead_email", None)

        for key, value in update_data.items():
            setattr(project, key, value)

        session.add(project)
        session.commit()
        session.refresh(project)
        lead = session.exec(select(User).where(User.unique_id == project.project_lead_orcid)).one_or_none() if project.project_lead_orcid else None
        return ProjectWithLead(**project.model_dump(), lead=lead)


def _get_project_users(proj_id: str, session) -> List[User]:
    '''Query project members directly — no auth check, for internal use only.'''
    return session.exec(
        select(User)
        .select_from(join(User, UserProjectLink, UserProjectLink.user_id == User.id))
        .where(UserProjectLink.project_id == proj_id)
    ).all()


@router.get("/projects/{proj_id}/users", response_model=List[UserRead])
def read_users_on_project(proj_id: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    '''
    Returns a list of users associated with the project ID.
    Accessible by admins and members of the project.
    '''
    user_unique_id, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        project = session.exec(select(Project).where(Project.project_id == proj_id)).one_or_none()
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")

        if not user_unique_id.startswith('admin'):
            if is_orcid(user_unique_id):
                member = session.exec(
                    select(UserProjectLink).where(
                        UserProjectLink.project_id == proj_id,
                        UserProjectLink.user_id == select(User.id).where(User.unique_id == user_unique_id).scalar_subquery()
                    )
                ).one_or_none()
                if member is None:
                    raise HTTPException(status_code=403, detail="Not a member of this project")
            else:
                raise HTTPException(status_code=403, detail="Not authorized")

        return _get_project_users(proj_id, session)


@router.post("/projects/{proj_id}/users/{orcid}", response_model=List[UserRead])
def add_users_to_project(proj_id: str, orcid: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    user_unique_id, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]
    
    with Session(engine) as session:
        # get user details
        user = session.exec(select(User).where(User.unique_id == orcid)).one_or_none()
        if user is None:
            raise HTTPException(status_code=404, detail = f'User {orcid} not found')
        
        # get project details
        proj = session.exec(select(Project).where(Project.project_id == proj_id)).one_or_none()
        if proj is None:
            raise HTTPException(status_code=404, detail = f'Project {proj_id} not found')
        
        # if the APIkey is not associated with an admin account
        # get requester details
        requester_notauth_detail = f'User must be the project lead or an admin to add other users to a project'
        if is_orcid(user_unique_id):
            requester = session.exec(select(User).where(User.unique_id == user_unique_id)).one_or_none()

            if not requester:
                raise HTTPException(status_code = 403, detail = requester_notauth_detail)

            if requester.unique_id != proj.project_lead_orcid:
                raise HTTPException(status_code=403, detail=requester_notauth_detail)

        # add link between user and project
        link = add_or_update(session,
                                UserProjectLink,
                                where=(and_(UserProjectLink.user_id == user.id, UserProjectLink.project_id == proj_id)),
                                user_id=user.id,
                                project_id=proj_id)
        session.commit()

        # add user to access group
        sql_add_user_to_access_group(orcid, proj_id, session)

        return _get_project_users(proj_id, session)


@router.delete("/projects/{proj_id}/users/{orcid}", response_model=List[UserRead])
def remove_user_from_project(proj_id: str, orcid: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    '''
    Remove a user from a project and its associated access group.
    Only admins or the project lead can remove users.
    '''
    user_unique_id, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        user = session.exec(select(User).where(User.unique_id == orcid)).one_or_none()
        if user is None:
            raise HTTPException(status_code=404, detail=f"User {orcid} not found")

        proj = session.exec(select(Project).where(Project.project_id == proj_id)).one_or_none()
        if proj is None:
            raise HTTPException(status_code=404, detail=f"Project {proj_id} not found")

        # Non-admins must be the project lead to remove users
        requester_notauth_detail = "User must be the project lead or an admin to remove users from a project"
        if is_orcid(user_unique_id):
            requester = session.exec(select(User).where(User.unique_id == user_unique_id)).one_or_none()
            if not requester:
                raise HTTPException(status_code=403, detail=requester_notauth_detail)
            if requester.unique_id != proj.project_lead_orcid:
                raise HTTPException(status_code=403, detail=requester_notauth_detail)

        # Remove the user-project link
        link = session.exec(
            select(UserProjectLink).where(
                UserProjectLink.user_id == user.id,
                UserProjectLink.project_id == proj_id
            )
        ).one_or_none()
        if link is None:
            raise HTTPException(status_code=404, detail=f"User {orcid} is not a member of project {proj_id}")
        session.delete(link)

        # Remove user from the project's access group
        ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == proj_id)).one_or_none()
        if ag:
            ag_link = session.exec(
                select(UserAccessGroupLink).where(
                    UserAccessGroupLink.user_id == user.id,
                    UserAccessGroupLink.accessgroup_id == ag.id
                )
            ).one_or_none()
            if ag_link:
                session.delete(ag_link)

        session.commit()
        return _get_project_users(proj_id, session)


@router.get("/projects/{project_id}/sample_graph")
def project_sample_graph(project_id: str, auth_res: Annotated[tuple, Depends(authorize_user)]):

    user_unique_id, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        samp_cmd = select(Sample).options(joinedload(Sample.parents))

        # Admin API keys can see all samples
        if user_unique_id.startswith('admin'):
            print('using admin account')
            pass
        elif is_orcid(user_unique_id):
            print(f'filtering on owner_orcid={user_unique_id}')
            associated_group_names = [ag.group_name for ag in ag_list]
            samp_cmd = samp_cmd.where(or_(Sample.owner_orcid == user_unique_id,
                                          Sample.project_id.in_(associated_group_names)))
        else:
            print(f'apikey does not have access to samples')
            return []

        # Filter samples on proj_id
        samp_cmd = samp_cmd.where(Sample.project_id == project_id)
        samp_list = session.exec(samp_cmd).unique().all()

        edges = []
        for sample in samp_list:
            for parent in sample.parents:
                edges.append((parent.unique_id, sample.unique_id))

        G = nx.DiGraph(edges)
        for sample in samp_list:
            G.add_node(sample.unique_id,
                       sample_name=sample.sample_name)

        return json_graph.node_link_data(G)

@router.get("/projects/{project_id}/dataset_graph")
def project_dataset_graph(project_id: str, auth_res: Annotated[tuple, Depends(authorize_user)]):

    user_unique_id, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        auth_filter = (DatasetACL.accessgroup_id.in_(ag_ids)) | (Dataset.public == True)

        ds_cmd = (
            select(Dataset)
            .options(joinedload(Dataset.parents))
            .join(DatasetACL, DatasetACL.dataset_id == Dataset.id, isouter=True)
            .filter(auth_filter)
            .where(Dataset.project_id == project_id)
        )

        ds_list = session.exec(ds_cmd).unique().all()

        edges = []
        for dataset in ds_list:
            for parent in dataset.parents:
                edges.append((parent.unique_id, dataset.unique_id))

        G = nx.DiGraph(edges)
        for dataset in ds_list:
            G.add_node(dataset.unique_id,
                       dataset_name=dataset.dataset_name)

        return json_graph.node_link_data(G)


@router.get("/projects/{project_id}/entity_graph")
def project_entity_graph(project_id: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    '''
    Returns a NetworkX node-link graph of all samples and datasets in the
    project, connected via sample lineage, dataset lineage, and sample-dataset
    associations. Nodes carry entity_type ('sample' or 'dataset') and name.
    '''
    user_unique_id, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        # Load all samples in the project with parent relationships
        samp_cmd = select(Sample).options(joinedload(Sample.parents))
        if not user_unique_id.startswith('admin'):
            if is_orcid(user_unique_id):
                associated_groups = session.exec(select(AccessGroup).where(AccessGroup.id.in_(ag_ids))).all()
                associated_group_names = [ag.group_name for ag in associated_groups]
                samp_cmd = samp_cmd.where(or_(Sample.owner_orcid == user_unique_id,
                                              Sample.project_id.in_(associated_group_names)))
            else:
                return json_graph.node_link_data(nx.DiGraph())
        samp_cmd = samp_cmd.where(Sample.project_id == project_id)
        samp_list = session.exec(samp_cmd).unique().all()

        # Load all authorized datasets in the project with parent relationships
        auth_filter = (DatasetACL.accessgroup_id.in_(ag_ids)) | (Dataset.public == True)
        ds_list = session.exec(
            select(Dataset)
            .options(joinedload(Dataset.parents))
            .join(DatasetACL, DatasetACL.dataset_id == Dataset.id, isouter=True)
            .filter(auth_filter)
            .where(Dataset.project_id == project_id)
        ).unique().all()

        s_int_to_uid = {s.id: s.unique_id for s in samp_list}
        d_int_to_uid = {d.id: d.unique_id for d in ds_list}

        # Load sample-dataset associations scoped to this project's entities
        sd_links = session.exec(
            select(DatasetSampleLink)
            .where(DatasetSampleLink.sample_id.in_(s_int_to_uid.keys()))
            .where(DatasetSampleLink.dataset_id.in_(d_int_to_uid.keys()))
        ).all() if s_int_to_uid and d_int_to_uid else []

        # Build combined graph — no subgraph extraction needed, include everything
        G = nx.DiGraph()
        for s in samp_list:
            G.add_node(s.unique_id, entity_type='sample', name=s.sample_name)
            for parent in s.parents:
                G.add_edge(parent.unique_id, s.unique_id)
        for d in ds_list:
            G.add_node(d.unique_id, entity_type='dataset', name=d.dataset_name)
            for parent in d.parents:
                G.add_edge(parent.unique_id, d.unique_id)
        for link in sd_links:
            G.add_edge(s_int_to_uid[link.sample_id], d_int_to_uid[link.dataset_id])

        return json_graph.node_link_data(G)


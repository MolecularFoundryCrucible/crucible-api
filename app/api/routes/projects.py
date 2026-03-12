from typing import Annotated, List, Union

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import join
from sqlalchemy.orm import joinedload
from sqlmodel import and_, or_, Session, select
import networkx as nx
from networkx.readwrite import json_graph

from app.api.dependencies import get_engine
from app.general_utils import is_orcid
from app.models.core_models import (
    AccessGroup, Project, Sample, User, UserProjectLink, DatasetACL, Dataset, Sample, DatasetSampleLink
)
from app.utils.auth_utils import authorize_user, is_admin
from app.utils.sql_utils import add_or_update
from app.utils.user_utils import sql_add_user_to_access_group
from app.utils.project_utils import sql_get_projects_for_user

router = APIRouter(tags=["projects"])
engine = get_engine()


@router.post("/projects", response_model=Project)
def create_project(auth_res: Annotated[tuple, Depends(authorize_user)], P: Project):
    """
    Add or update a project record in the database.

    Args:
        auth_res: ACL info for user
        P: Project object to be added or updated

    Returns:
        Added or updated project object

    Raises:
        HTTPException: If required project fields are missing
    """
    # Validate required fields before attempting SQL operations
    missing_fields = []
    if not P.project_id or not P.project_id.strip():
        missing_fields.append("project_id")
    if not P.organization or not P.organization.strip():
        missing_fields.append("organization")
    if not P.project_lead_email or not P.project_lead_email.strip():
        missing_fields.append("project_lead_email")

    if missing_fields:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required project information: {', '.join(missing_fields)}. Please provide all required fields: project_id, organization, and project_lead_email."
        )

    with Session(engine) as session:
        prop = add_or_update(session, Project, Project.id == P.id, **dict(P))
        ag = add_or_update(session,
                           AccessGroup,
                           AccessGroup.group_name == P.project_id,
                           group_name=P.project_id)

        r1 = session.exec(select(User).where(User.lbl_email.ilike(f"%{P.project_lead_email}%"))).all()
        r2 = session.exec(select(User).where(User.email.ilike(f"%{P.project_lead_email}%"))).all()
        all_found_accounts = [x for x in r1 + r2 if x.orcid is not None]

        print(f"found accounts for user: {all_found_accounts}")

        if len(all_found_accounts) > 0:
            project_lead = all_found_accounts[-1]
            if not (project_lead in ag.users):
                ag.users.append(project_lead)

            if not (prop in project_lead.projects):
                project_lead.projects.append(prop)

        session.commit()
    return(P)


@router.get("/projects", response_model=List[Project])
def list_projects(auth_res: Annotated[tuple, Depends(authorize_user)]):
    '''
    Returns a list of projects the user is authorized to see
    '''
    access_group_name, ag_ids = auth_res
    if access_group_name.startswith('admin'):
        with Session(engine) as session:
            props = session.exec(select(Project)).all()
            return(props)

    elif is_orcid(access_group_name):
        with Session(engine) as session:
            projects = sql_get_projects_for_user(access_group_name, session)
            return projects if projects is not None else []

    else:
        raise HTTPException(status_code=403, detail="Not authorized to view projects")


@router.get("/projects/{proj_id}", response_model=Union[None, Project])
def read_project(proj_id: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    '''
    Returns the project object for the provided ID
    '''
    with Session(engine) as session:
        prop = session.exec(select(Project).where(Project.project_id == proj_id)).one_or_none()
        return(prop)


@router.get("/projects/{proj_id}/users", response_model=List[User])
def read_users_on_project(proj_id: str, admin_acct: Annotated[str, Depends(is_admin)]):
    '''
    Returns a list of users associated with the project ID provided
    '''
    with Session(engine) as session:
        # Verify project exists
        project = session.exec(select(Project).where(Project.project_id == proj_id)).one_or_none()

        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")

        users = session.exec(select(User).select_from(join(User, UserProjectLink, UserProjectLink.user_id == User.id)).where(UserProjectLink.project_id == proj_id)).all()
        return users


@router.post("/projects/{proj_id}/users/{orcid}", response_model=List[User])
def add_users_to_project(proj_id: str, orcid: str, admin_acct: Annotated[str, Depends(is_admin)]):

    with Session(engine) as session:
        user = session.exec(select(User).where(User.orcid == orcid)).one_or_none()
        if user:
            # add link between user and project
            link = add_or_update(session,
                                 UserProjectLink,
                                 where=(and_(UserProjectLink.user_id == user.id, UserProjectLink.project_id == proj_id)),
                                 user_id=user.id,
                                 project_id=proj_id)
            session.commit()

        # add user to access group
        sql_add_user_to_access_group(orcid, proj_id, session)

    project_users = read_users_on_project(proj_id, admin_acct)
    return project_users


@router.get("/projects/{project_id}/sample_graph")
def project_sample_graph(project_id: str, auth_res: Annotated[tuple, Depends(authorize_user)]):

    access_group_name, ag_ids = auth_res

    with Session(engine) as session:
        samp_cmd = select(Sample).options(joinedload(Sample.parents))

        # Admin API keys can see all samples
        if access_group_name.startswith('admin'):
            print('using admin account')
            pass
        elif is_orcid(access_group_name):
            print(f'filtering on owner_orcid={access_group_name}')
            associated_groups = session.exec(select(AccessGroup).where(AccessGroup.id.in_(ag_ids))).all()
            associated_group_names = [ag.group_name for ag in associated_groups]
            samp_cmd = samp_cmd.where(or_(Sample.owner_orcid == access_group_name,
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

    access_group_name, ag_ids = auth_res

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
    access_group_name, ag_ids = auth_res

    with Session(engine) as session:
        # Load all samples in the project with parent relationships
        samp_cmd = select(Sample).options(joinedload(Sample.parents))
        if not access_group_name.startswith('admin'):
            if is_orcid(access_group_name):
                associated_groups = session.exec(select(AccessGroup).where(AccessGroup.id.in_(ag_ids))).all()
                associated_group_names = [ag.group_name for ag in associated_groups]
                samp_cmd = samp_cmd.where(or_(Sample.owner_orcid == access_group_name,
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


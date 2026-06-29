from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, join, literal, text
from sqlalchemy.orm import joinedload, selectinload
from sqlmodel import and_, or_, Session, select, col
import networkx as nx
from networkx.readwrite import json_graph

from app.api.dependencies import get_engine, get_trgm_search_threshold
from app.general_utils import is_orcid
from app.models.core_models import (
    AccessGroup, Project, Resource, Sample, User, UserProjectLink, UserAccessGroupLink,
    ResourceACL, Dataset, DatasetSampleLink,
)
from app.utils.auth_utils import authorize_user, require_groups
from app.utils.sql_utils import add_or_update
from app.utils.user_utils import sql_add_user_to_access_group, resolve_user
from app.utils.project_utils import sql_get_projects_for_user
from app.models.pagination import PaginatedResponse
from app.models.projects import ProjectCreate, ProjectRead, ProjectUpdate, ProjectWithLead
from app.models.users import UserRead

router = APIRouter(tags=["projects"])
engine = get_engine()


@router.post("/projects", response_model=ProjectRead)
def create_project(auth_res: Annotated[tuple, Depends(authorize_user)], P: ProjectCreate):
    """
    Create a new project. Either project_lead_orcid or project_lead_email must be
    provided and must correspond to an existing user. The resolved ORCID is stored on the project.
    Returns 409 if a project with the given project_id already exists.
    """
    if not P.project_lead_orcid and not P.project_lead_email and not P.project_lead_username:
        raise HTTPException(status_code=400, detail="One of project_lead_orcid, project_lead_email, or project_lead_username must be provided.")

    with Session(engine) as session:
        existing = session.exec(select(Project).where(Project.project_id == P.project_id)).one_or_none()
        if existing is not None:
            raise HTTPException(status_code=409, detail=f"Project '{P.project_id}' already exists. Use PATCH to update it.")

        project_lead = resolve_user(session, orcid=P.project_lead_orcid, email=P.project_lead_email, username=P.project_lead_username)

        prop = Project(
            project_id=P.project_id,
            organization=P.organization,
            status=P.status,
            title=P.title,
            project_lead_orcid=project_lead.unique_id,
            project_lead_email=project_lead.email,
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


@router.get("/projects", response_model=PaginatedResponse[ProjectWithLead])
def list_projects(auth_res: Annotated[tuple, Depends(authorize_user)],
                  include_metadata: bool = False,
                  limit: int = Query(default=100, ge=1, le=1000),
                  offset: int = Query(default=0, ge=0)):
    '''
    Returns a paginated list of projects the user is authorized to see, including the resolved project lead.
    '''
    user_unique_id, ag_list = auth_res

    if include_metadata:
        loader_opts = [selectinload(Project.resource).selectinload(Resource.scientific_metadata)]
    else:
        loader_opts = [selectinload(Project.resource)]

    with Session(engine) as session:
        if is_orcid(user_unique_id):
            user = session.exec(select(User).where(User.unique_id == user_unique_id)).one_or_none()
            if user is None:
                return PaginatedResponse(total=0, limit=limit, offset=offset, items=[])
            base = (
                select(Project)
                .join(UserProjectLink, Project.project_id == UserProjectLink.project_id)
                .where(UserProjectLink.user_id == user.id)
            )
        else:
            require_groups(ag_list)
            base = select(Project)

        total = session.exec(select(func.count()).select_from(base.subquery())).one()

        props = session.exec(
            base.options(*loader_opts).order_by(col(Project.project_id)).offset(offset).limit(limit)
        ).all()

        orcids = [p.project_lead_orcid for p in props if p.project_lead_orcid]
        leads = {u.unique_id: u for u in session.exec(select(User).where(User.unique_id.in_(orcids))).all()}

        items = []
        for proj in props:
            resource_md = proj.resource.scientific_metadata if proj.resource else None
            items.append(ProjectWithLead(
                **proj.model_dump(),
                lead=leads.get(proj.project_lead_orcid),
                creation_time=proj.resource.creation_time if proj.resource else None,
                modification_time=proj.resource.modification_time if proj.resource else None,
                scientific_metadata=resource_md.scientific_metadata if include_metadata and resource_md else None,
            ))
        return PaginatedResponse(total=total, limit=limit, offset=offset, items=items)


def _project_base_query(user_unique_id: str, ag_list: list, session):
    """Return a base select for projects visible to the user (same scoping as list_projects)."""
    if is_orcid(user_unique_id):
        user = session.exec(select(User).where(User.unique_id == user_unique_id)).one_or_none()
        if user is None:
            return None
        return (
            select(Project)
            .join(UserProjectLink, Project.project_id == UserProjectLink.project_id)
            .where(UserProjectLink.user_id == user.id)
        )
    else:
        require_groups(ag_list)
        return select(Project)


@router.get("/projects/search", response_model=PaginatedResponse[ProjectRead])
def search_projects(
    auth_res: Annotated[tuple, Depends(authorize_user)],
    q: str = Query(min_length=3, description="Search term matched against project title and project_id."),
    limit: int = Query(default=20, ge=1, le=50),
):
    '''
    Search projects by title or project_id. Returns results ordered by match quality.
    Scoped to projects the caller is a member of. Maximum 50 results.
    '''
    user_unique_id, ag_list = auth_res

    with Session(engine) as session:
        base = _project_base_query(user_unique_id, ag_list, session)
        if base is None:
            return PaginatedResponse(limit=limit, items=[])

        session.execute(text(f"SET LOCAL pg_trgm.word_similarity_threshold = {get_trgm_search_threshold()}"))
        query = base.where(
            literal(q).op("<<%")(Project.project_id) |
            literal(q).op("<<%")(Project.title)
        ).order_by(
            func.greatest(
                func.strict_word_similarity(q, Project.project_id),
                func.coalesce(func.strict_word_similarity(q, Project.title), 0),
            ).desc()
        ).limit(limit)

        results = session.exec(query).all()
        return PaginatedResponse(limit=limit, items=results)


@router.get("/projects/{proj_id}", response_model=ProjectWithLead)
def read_project(proj_id: str, auth_res: Annotated[tuple, Depends(authorize_user)], include_metadata: bool = False):
    '''
    Returns the project object for the provided ID, including the resolved project lead.
    '''
    user_unique_id, ag_list = auth_res
    require_groups(ag_list, {'admin', proj_id})

    with Session(engine) as session:
        query = select(Project).where(Project.project_id == proj_id)
        if include_metadata:
            query = query.options(selectinload(Project.resource).selectinload(Resource.scientific_metadata))
        else:
            query = query.options(selectinload(Project.resource))
        prop = session.exec(query).one_or_none()
        if prop is None:
            raise HTTPException(status_code=404, detail="Project not found")
        lead = session.exec(select(User).where(User.unique_id == prop.project_lead_orcid)).one_or_none() if prop.project_lead_orcid else None
        resource_md = prop.resource.scientific_metadata if prop.resource else None
        return ProjectWithLead(
            **prop.model_dump(),
            lead=lead,
            creation_time=prop.resource.creation_time if prop.resource else None,
            modification_time=prop.resource.modification_time if prop.resource else None,
            scientific_metadata=resource_md.scientific_metadata if include_metadata and resource_md else None,
        )


@router.patch("/projects/{proj_id}", response_model=ProjectWithLead)
def update_project(proj_id: str, auth_res: Annotated[tuple, Depends(authorize_user)], project_update: ProjectUpdate):
    '''
    Partially update a project record. Only fields provided in the request body are modified.
    Accessible by admins and the project lead.
    If project_lead_orcid or project_lead_email is provided, it must correspond to an existing user.
    '''
    user_unique_id, ag_list = auth_res
    require_groups(ag_list, {'admin', proj_id})

    with Session(engine) as session:
        project = session.exec(select(Project).where(Project.project_id == proj_id)).one_or_none()

        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        
        update_data = project_update.model_dump(exclude_unset=True)

        lead_orcid = update_data.pop("project_lead_orcid", None)
        lead_email = update_data.pop("project_lead_email", None)
        lead_username = update_data.pop("project_lead_username", None)

        if any([lead_orcid, lead_email, lead_username]):
            lead = resolve_user(session, orcid=lead_orcid, email=lead_email, username=lead_username)
            update_data["project_lead_orcid"] = lead.unique_id
            update_data["project_lead_email"] = lead.email

        for key, value in update_data.items():
            setattr(project, key, value)

        session.add(project)
        session.commit()
        session.refresh(project)
        lead = session.exec(select(User).where(User.unique_id == project.project_lead_orcid)).one_or_none() if project.project_lead_orcid else None
        resource = session.exec(select(Resource).where(Resource.mfid == proj_id)).one_or_none()
        return ProjectWithLead(
            **project.model_dump(),
            lead=lead,
            creation_time=resource.creation_time if resource else None,
            modification_time=resource.modification_time if resource else None,
        )


def _get_project_users(proj_id: str, session) -> List[User]:
    '''Query project members directly — no auth check, for internal use only.'''
    return session.exec(
        select(User)
        .select_from(join(User, UserProjectLink, UserProjectLink.user_id == User.id))
        .where(UserProjectLink.project_id == proj_id)
    ).all()


@router.get("/projects/{proj_id}/users", response_model=PaginatedResponse[UserRead])
def read_users_on_project(proj_id: str, auth_res: Annotated[tuple, Depends(authorize_user)],
                          limit: int = Query(default=100, ge=1, le=1000),
                          offset: int = Query(default=0, ge=0)):
    '''
    Returns a paginated list of users associated with the project ID.
    Accessible by admins and members of the project.
    '''
    _, ag_list = auth_res

    require_groups(ag_list, {'admin', proj_id})

    with Session(engine) as session:
        project = session.exec(select(Project).where(Project.project_id == proj_id)).one_or_none()
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")

        all_users = _get_project_users(proj_id, session)
        return PaginatedResponse(total=len(all_users), limit=limit, offset=offset, items=all_users[offset:offset + limit])


@router.post("/projects/{proj_id}/users/{orcid}", response_model=List[UserRead])
def add_users_to_project(
    proj_id: str,
    orcid: str,
    auth_res: Annotated[tuple, Depends(authorize_user)],
    email: Optional[str] = None,
    username: Optional[str] = None,
):
    '''
    Add a user to a project. Provide ?email= or ?username= to resolve the user instead of
    the orcid path parameter. Only admins or project members can add users.
    '''
    _, ag_list = auth_res
    require_groups(ag_list, {'admin', proj_id})

    with Session(engine) as session:
        user = resolve_user(session, orcid=orcid if not email and not username else None, email=email, username=username)

        proj = session.exec(select(Project).where(Project.project_id == proj_id)).one_or_none()
        if proj is None:
            raise HTTPException(status_code=404, detail=f'Project {proj_id} not found')

        add_or_update(session,
                      UserProjectLink,
                      where=(and_(UserProjectLink.user_id == user.id, UserProjectLink.project_id == proj_id)),
                      user_id=user.id,
                      project_id=proj_id)

        sql_add_user_to_access_group(user.unique_id, proj_id, session)
        session.commit()

        return _get_project_users(proj_id, session)


@router.delete("/projects/{proj_id}/users/{orcid}", response_model=List[UserRead])
def remove_user_from_project(proj_id: str, orcid: str, auth_res: Annotated[tuple, Depends(authorize_user)],
                             email: Optional[str] = None, username: Optional[str] = None):
    '''
    Remove a user from a project and its associated access group.
    Only admins or the project lead can remove users.
    Provide ?email= or ?username= to look up the user instead of the orcid path parameter.
    '''
    _, ag_list = auth_res
    require_groups(ag_list, {'admin', proj_id})
    with Session(engine) as session:
        user = resolve_user(session, orcid=orcid if not email and not username else None, email=email, username=username)

        proj = session.exec(select(Project).where(Project.project_id == proj_id)).one_or_none()
        if proj is None:
            raise HTTPException(status_code=404, detail=f"Project {proj_id} not found")

        # Remove the user-project link
        link = session.exec(
            select(UserProjectLink).where(
                UserProjectLink.user_id == user.id,
                UserProjectLink.project_id == proj_id
            )
        ).one_or_none()
        if link is None:
            raise HTTPException(status_code=404, detail=f"User {user.unique_id} is not a member of project {proj_id}")
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

    _, ag_list = auth_res
    require_groups(ag_list, {'admin', project_id})

    with Session(engine) as session:
        samp_cmd = select(Sample).options(joinedload(Sample.parents))

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

    _, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        auth_filter = (ResourceACL.accessgroup_id.in_(ag_ids)) | (Dataset.public == True)

        ds_cmd = (
            select(Dataset)
            .options(joinedload(Dataset.parents))
            .join(ResourceACL, ResourceACL.mfid == Dataset.unique_id, isouter=True)
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
    _, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]
    require_groups(ag_list, {'admin', project_id})

    with Session(engine) as session:
        # Load all samples in the project with parent relationships
        samp_cmd = select(Sample).options(joinedload(Sample.parents)).where(Sample.project_id == project_id)
        samp_list = session.exec(samp_cmd).unique().all()

        # Load all authorized datasets in the project with parent relationships
        auth_filter = (ResourceACL.accessgroup_id.in_(ag_ids)) | (Dataset.public == True)
        ds_list = session.exec(
            select(Dataset)
            .options(joinedload(Dataset.parents))
            .join(ResourceACL, ResourceACL.mfid == Dataset.unique_id, isouter=True)
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


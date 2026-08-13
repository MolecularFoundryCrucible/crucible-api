import os
from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func, join, literal, text
from sqlalchemy.orm import joinedload, selectinload
from sqlmodel import Session, select, col
import networkx as nx
from networkx.readwrite import json_graph

from app.api.dependencies import get_engine, get_trgm_search_threshold
from app.general_utils import is_orcid
from app.models.core_models import (
    AccessGroup, Project, Resource, Sample, User, UserProjectLink, UserAccessGroupLink,
    ResourceACL, Dataset, DatasetSampleLink,
)
from app.utils.auth_utils import (
    authorize_user, permission_filter, require_grant, require_permission,
    require_platform_admin)
from app.utils.permissions import (
    Permission, Principal, Role, can_grant, effective_role)
from app.utils.sql_utils import add_or_update, sql_create_resource_acl, touch_resource_modification_time
from app.utils.user_utils import resolve_user, sql_add_user_to_access_group
from app.utils.project_utils import (
    assert_project_id_available, is_project_owner, sql_add_user_to_project,
    sql_get_projects_for_user)
from app.models.access_groups import AccessGroupMemberRead
from app.models.pagination import PaginatedResponse
from app.models.projects import ProjectCreate, ProjectRead, ProjectUpdate, ProjectWithLead
from app.models.users import UserPublicRead, UserRead

router = APIRouter(tags=["projects"])
engine = get_engine()

API_VERSION = os.getenv('API_VERSION', None)
API_VERSION_GITHASH = os.getenv("GITHASH", None)


@router.post("/projects", response_model=ProjectRead)
def create_project(auth_res: Annotated[Principal, Depends(authorize_user)], P: ProjectCreate):
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

        # The project does not exist, so any access group already holding this
        # name belongs to something else. add_or_update below would reuse it.
        assert_project_id_available(session, P.project_id)

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

        # ag.users.append would insert a link row with no role, failing closed to viewer.
        # Group is keyed by the project's MFID (unique_id), not project_id, see
        # docs/permission_system_plan.md, "Groups" / the MFID-named-groups rename.
        ag = sql_add_user_to_access_group(project_lead.unique_id, prop.unique_id,
                                          session, role=Role.OWNER)

        add_or_update(session, Resource,
                      where=(Resource.mfid == P.project_id),
                      mfid=P.project_id,
                      resource_type="project",
                      created_with_api_version=API_VERSION,
                      api_version_githash=API_VERSION_GITHASH)
        sql_create_resource_acl(ag, P.project_id, read=True, write=True,
                                session=session, role=Role.OWNER)

        if prop not in project_lead.projects:
            project_lead.projects.append(prop)

        session.commit()
        session.refresh(prop)
        return prop


@router.get("/projects", response_model=PaginatedResponse[ProjectWithLead])
def list_projects(auth_res: Annotated[Principal, Depends(authorize_user)],
                  include_metadata: bool = False,
                  limit: int = Query(default=100, ge=1, le=1000),
                  offset: int = Query(default=0, ge=0)):
    '''
    Returns a paginated list of projects the user is authorized to see, including the resolved project lead.
    '''
    user_unique_id = auth_res.user_id
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
                .join(AccessGroup, AccessGroup.group_name == Project.unique_id)
                .join(UserAccessGroupLink, and_(
                    UserAccessGroupLink.accessgroup_id == AccessGroup.id,
                    UserAccessGroupLink.user_id == user.id,
                ))
            )
        else:
            require_platform_admin(auth_res)
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


@router.get("/projects/search", response_model=PaginatedResponse[ProjectRead])
def search_projects(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    q: str = Query(min_length=3, description="Search term matched against project title and project_id."),
    limit: int = Query(default=20, ge=1, le=50),
):
    '''
    Search projects by title or project_id across all projects, not just ones the
    caller is a member of — this is how a user discovers a project to request to
    join via POST /access_groups/{group_name}/join. Returns results ordered by
    match quality. Maximum 50 results.

    Note: ProjectRead has no lead/scientific_metadata fields, so this is safe to
    open regardless of membership — see GET /projects/{proj_id} for the fields
    that stay member-only.
    '''
    with Session(engine) as session:
        session.execute(text(f"SET LOCAL pg_trgm.word_similarity_threshold = {get_trgm_search_threshold()}"))
        tokens = q.lower().split()
        query = select(Project)
        for token in tokens:
            query = query.where(
                Project.project_id.ilike(f"{token}%") |
                literal(token).op("<%")(Project.project_id) |
                literal(token).op("<%")(Project.title)
            )
        query = query.order_by(
            func.greatest(
                func.word_similarity(q, Project.project_id),
                func.coalesce(func.word_similarity(q, Project.title), 0),
            ).desc()
        ).limit(limit)

        results = session.exec(query).all()
        return PaginatedResponse(limit=limit, items=results)


@router.get("/projects/{proj_id}", response_model=ProjectWithLead)
def read_project(proj_id: str, auth_res: Annotated[Principal, Depends(authorize_user)], include_metadata: bool = False):
    '''
    Returns the project object for the provided ID, including the resolved project lead.
    Any authenticated user can read basic project info (project_id, organization,
    status, title) to support discovery for join requests. Members and admins
    additionally get the lead's email (UserRead) and scientific_metadata; non-members
    get the lead as UserPublicRead (no email) and scientific_metadata is always null,
    regardless of ?include_metadata=.
    '''
    user_unique_id, ag_list = auth_res.user_id, auth_res.groups
    is_member = any(ag.group_name in ('admin', proj_id) for ag in ag_list)

    with Session(engine) as session:
        query = select(Project).where(Project.project_id == proj_id)
        if include_metadata and is_member:
            query = query.options(selectinload(Project.resource).selectinload(Resource.scientific_metadata))
        else:
            query = query.options(selectinload(Project.resource))
        prop = session.exec(query).one_or_none()
        if prop is None:
            raise HTTPException(status_code=404, detail="Project not found")
        lead_user = session.exec(select(User).where(User.unique_id == prop.project_lead_orcid)).one_or_none() if prop.project_lead_orcid else None
        lead = (UserRead.model_validate(lead_user) if is_member else UserPublicRead.model_validate(lead_user)) if lead_user else None
        resource_md = prop.resource.scientific_metadata if (prop.resource and is_member) else None
        return ProjectWithLead(
            **prop.model_dump(),
            lead=lead,
            creation_time=prop.resource.creation_time if prop.resource else None,
            modification_time=prop.resource.modification_time if prop.resource else None,
            scientific_metadata=resource_md.scientific_metadata if include_metadata and resource_md else None,
        )


@router.patch("/projects/{proj_id}", response_model=ProjectWithLead)
def update_project(proj_id: str, auth_res: Annotated[Principal, Depends(authorize_user)], project_update: ProjectUpdate):
    '''
    Partially update a project record. Only fields provided in the request body are modified.
    Accessible by admins and the project lead.
    Leadership is not a field here — ProjectUpdate has no project_lead_*
    fields (extra="forbid" 422s if you send one). Use
    POST /resources/{proj_id}/transfer_ownership instead.
    '''
    with Session(engine) as session:
        project = session.exec(select(Project).where(Project.project_id == proj_id)).one_or_none()

        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")

        require_permission(session, auth_res, proj_id, Permission.EDIT, legacy_group=project.unique_id)

        update_data = project_update.model_dump(exclude_unset=True)

        for key, value in update_data.items():
            setattr(project, key, value)

        session.add(project)
        touch_resource_modification_time(session, proj_id)
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


def _get_project_users(proj_id: str, session) -> List[AccessGroupMemberRead]:
    '''Query project members and their role from the project's own access
    group — the authoritative membership record, not UserProjectLink (see
    is_project_member). No auth check, for internal use only.'''
    rows = session.exec(
        select(User, UserAccessGroupLink.role)
        .select_from(join(AccessGroup, UserAccessGroupLink, UserAccessGroupLink.accessgroup_id == AccessGroup.id))
        .join(User, User.id == UserAccessGroupLink.user_id)
        .join(Project, Project.unique_id == AccessGroup.group_name)
        .where(Project.project_id == proj_id)
    ).all()
    return [
        AccessGroupMemberRead.model_validate(user, update={"role": role or "unknown"})
        for user, role in rows
    ]


@router.get("/projects/{proj_id}/users", response_model=PaginatedResponse[AccessGroupMemberRead])
def read_users_on_project(proj_id: str, auth_res: Annotated[Principal, Depends(authorize_user)],
                          limit: int = Query(default=100, ge=1, le=1000),
                          offset: int = Query(default=0, ge=0)):
    '''
    Returns a paginated list of users associated with the project ID.
    Accessible by admins and members of the project.
    '''
    with Session(engine) as session:
        project = session.exec(select(Project).where(Project.project_id == proj_id)).one_or_none()
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")

        require_permission(session, auth_res, proj_id, Permission.READ, legacy_group=project.unique_id)

        all_users = _get_project_users(proj_id, session)
        return PaginatedResponse(total=len(all_users), limit=limit, offset=offset, items=all_users[offset:offset + limit])


@router.post("/projects/{proj_id}/users/{orcid}", response_model=List[AccessGroupMemberRead])
def add_users_to_project(
    proj_id: str,
    orcid: str,
    auth_res: Annotated[Principal, Depends(authorize_user)],
    email: Optional[str] = None,
    username: Optional[str] = None,
    role: Role = Role.CONTRIBUTOR,
):
    '''
    Add a user to a project at `role`, defaulting to contributor.

    Requires editor or above in the project, and you may only grant a role at
    or below your own — so an editor can seat a contributor but never an admin.
    '''
    with Session(engine) as session:
        proj = session.exec(select(Project).where(Project.project_id == proj_id)).one_or_none()
        if proj is None:
            raise HTTPException(status_code=404, detail=f'Project {proj_id} not found')

        require_permission(session, auth_res, proj_id, Permission.EDIT, legacy_group=proj.unique_id)
        require_grant(session, auth_res, proj_id, role, floor=Role.EDITOR)

        user = resolve_user(session, orcid=orcid if not email and not username else None, email=email, username=username)

        sql_add_user_to_project(user, proj_id, session, role=role)
        session.commit()

        return _get_project_users(proj_id, session)


@router.patch("/projects/{proj_id}/users/{orcid}", response_model=List[AccessGroupMemberRead])
def set_project_user_role(
    proj_id: str,
    orcid: str,
    auth_res: Annotated[Principal, Depends(authorize_user)],
    role: Role,
):
    '''
    Change a member's role in a project.

    The cap binds on both ends: you may not grant a role above your own, nor
    change a member who already holds one above your own. That is what stops a
    project admin demoting the owner.
    '''
    with Session(engine) as session:
        proj = session.exec(select(Project).where(Project.project_id == proj_id)).one_or_none()
        if proj is None:
            raise HTTPException(status_code=404, detail=f'Project {proj_id} not found')

        require_permission(session, auth_res, proj_id, Permission.EDIT, legacy_group=proj.unique_id)

        user = resolve_user(session, orcid=orcid)
        ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == proj.unique_id)).one_or_none()
        if ag is None:
            raise HTTPException(status_code=404, detail=f'Project {proj_id} not found')

        link = session.exec(
            select(UserAccessGroupLink)
            .where(UserAccessGroupLink.user_id == user.id)
            .where(UserAccessGroupLink.accessgroup_id == ag.id)
        ).one_or_none()
        if link is None:
            raise HTTPException(status_code=404,
                                detail=f'{orcid} is not a member of {proj_id}')

        current = Role.from_db(link.role) if link.role else None
        require_grant(session, auth_res, proj_id, role, current=current, floor=Role.EDITOR)

        link.role = role.db_value
        session.add(link)
        touch_resource_modification_time(session, proj_id)
        session.commit()

        return _get_project_users(proj_id, session)


@router.delete("/projects/{proj_id}/users/{orcid}", response_model=List[AccessGroupMemberRead])
def remove_user_from_project(proj_id: str, orcid: str, auth_res: Annotated[Principal, Depends(authorize_user)],
                             email: Optional[str] = None, username: Optional[str] = None):
    '''
    Remove a user from a project and its associated access group.
    Admins or the project lead can remove any member. A member can also
    remove themselves (leave the project), but the project lead cannot
    remove themselves — transfer leadership first.
    Provide ?email= or ?username= to look up the user instead of the orcid path parameter.
    '''
    user_unique_id, ag_list = auth_res.user_id, auth_res.groups
    is_admin = any(ag.group_name == 'admin' for ag in ag_list)

    with Session(engine) as session:
        proj = session.exec(select(Project).where(Project.project_id == proj_id)).one_or_none()
        if proj is None:
            raise HTTPException(status_code=404, detail=f"Project {proj_id} not found")

        is_self = user_unique_id == orcid and not email and not username
        caller_is_owner = is_project_owner(user_unique_id, proj_id, session)
        if not is_admin and not caller_is_owner and not is_self:
            raise HTTPException(status_code=403, detail="Only platform admins, the project lead, or the member themselves can remove a member.")

        if is_self and caller_is_owner:
            raise HTTPException(status_code=409, detail="The project lead cannot remove themselves. Transfer leadership first.")

        user = resolve_user(session, orcid=orcid if not email and not username else None, email=email, username=username)

        # The access-group standing is the real membership record — see
        # is_project_member. A missing UserAccessGroupLink means the user has
        # no actual access regardless of what UserProjectLink says.
        ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == proj.unique_id)).one_or_none()
        ag_link = session.exec(
            select(UserAccessGroupLink).where(
                UserAccessGroupLink.user_id == user.id,
                UserAccessGroupLink.accessgroup_id == ag.id,
            )
        ).one_or_none() if ag else None
        if ag_link is None:
            raise HTTPException(status_code=404, detail=f"User {user.unique_id} is not a member of project {proj_id}")
        session.delete(ag_link)

        # Best-effort cleanup of the legacy link table; its absence is not an error.
        link = session.exec(
            select(UserProjectLink).where(
                UserProjectLink.user_id == user.id,
                UserProjectLink.project_id == proj_id
            )
        ).one_or_none()
        if link is not None:
            session.delete(link)

        touch_resource_modification_time(session, proj_id)
        session.commit()
        return _get_project_users(proj_id, session)


@router.get("/projects/{project_id}/sample_graph")
def project_sample_graph(project_id: str, auth_res: Annotated[Principal, Depends(authorize_user)]):

    with Session(engine) as session:
        proj = session.exec(select(Project).where(Project.project_id == project_id)).one_or_none()
        require_permission(session, auth_res, project_id, Permission.READ,
                           legacy_group=proj.unique_id if proj else None)

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
def project_dataset_graph(project_id: str, auth_res: Annotated[Principal, Depends(authorize_user)]):

    ag_list = auth_res.groups
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
def project_entity_graph(project_id: str, auth_res: Annotated[Principal, Depends(authorize_user)]):
    '''
    Returns a NetworkX node-link graph of all samples and datasets in the
    project, connected via sample lineage, dataset lineage, and sample-dataset
    associations. Nodes carry entity_type ('sample' or 'dataset') and name.
    '''
    with Session(engine) as session:
        proj = session.exec(select(Project).where(Project.project_id == project_id)).one_or_none()
        require_permission(session, auth_res, project_id, Permission.READ,
                           legacy_group=proj.unique_id if proj else None)

        # Load all samples in the project with parent relationships
        samp_cmd = select(Sample).options(joinedload(Sample.parents)).where(Sample.project_id == project_id)
        samp_list = session.exec(samp_cmd).unique().all()

        # Load all authorized datasets in the project with parent relationships
        ds_list = session.exec(
            select(Dataset)
            .options(joinedload(Dataset.parents))
            .join(ResourceACL, ResourceACL.mfid == Dataset.unique_id, isouter=True)
            .filter(permission_filter(auth_res, Dataset))
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


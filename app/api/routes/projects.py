import os
from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import and_, func
from sqlalchemy.orm import joinedload, selectinload
from sqlmodel import Session, select, col
import networkx as nx
from networkx.readwrite import json_graph

from app.api.dependencies import get_engine, get_trgm_search_threshold
from app.models.core_models import (
    AccessGroup, Project, Resource, Sample, User, UserProjectLink, UserAccessGroupLink,
    Dataset, DatasetSampleLink,
)
from app.utils.auth_utils import (
    access_selector_filter, authorize_user, has_platform_admin_access,
    load_principal, permission_filter, project_owner_affiliation_filter,
    require_grant, require_permission,
    resource_capabilities)
from app.utils.permissions import (
    AccessGroupType, Permission, Principal, Role, can_grant, effective_role)
from app.utils.search_utils import (
    apply_trigram_search,
    configure_trigram_search,
    normalize_search_query,
    trigram_rank,
)
from app.utils.sql_utils import add_or_update, deletion_filter_for, sql_create_resource_acl, touch_resource_modification_time
from app.utils.user_utils import resolve_user, resolve_user_by_identifier, sql_add_user_to_access_group
from app.utils.project_utils import (
    assert_project_id_available, is_project_owner, sql_add_user_to_project,
    sql_get_projects_for_user, resolve_project_reference)
from app.models.access_groups import AccessGroupMemberRead
from app.models.pagination import PaginatedResponse
from app.models.query_params import ProjectListQuery, SearchQuery
from app.models.projects import (
    ProjectCreate, ProjectMembershipRole, ProjectRead, ProjectUpdate, ProjectWithLead)
from app.models.users import UserPublicRead
from app.services.resource_details import (
    ResourceDetailOptions,
    load_project_detail,
    load_project_members,
)

router = APIRouter(tags=["projects"])
engine = get_engine()

API_VERSION = os.getenv('API_VERSION', None)
API_VERSION_GITHASH = os.getenv("GITHASH", None)


def _project_capabilities(session, principal, project):
    return resource_capabilities(
        session,
        principal,
        project.unique_id,
        legacy_edit_group=project.unique_id,
        manage_access_floor=Role.EDITOR,
        allow_peer_grants=False,
        status_permission=Permission.EDIT,
        legacy_status_group=project.unique_id,
    )


@router.post("/projects", response_model=ProjectRead)
def create_project(auth_res: Annotated[Principal, Depends(authorize_user)], P: ProjectCreate):
    """
    Create a new project. One of project_lead (ORCID, username, or email) or
    project_lead_orcid/_email/_username must be provided and must correspond
    to an existing user. The resolved canonical user identifier is stored on the project.
    Returns 409 if a project with the given project_id already exists.
    """
    named_lead_fields = [P.project_lead, P.project_lead_orcid, P.project_lead_email, P.project_lead_username]
    if not any(named_lead_fields):
        raise HTTPException(status_code=400, detail="One of project_lead, project_lead_orcid, project_lead_email, or project_lead_username must be provided.")
    if P.project_lead and any(named_lead_fields[1:]):
        raise HTTPException(status_code=400, detail="Provide project_lead or one of project_lead_orcid/_email/_username, not both.")

    with Session(engine) as session:
        existing = session.exec(select(Project).where(Project.project_id == P.project_id)).one_or_none()
        if existing is not None:
            raise HTTPException(status_code=409, detail=f"Project '{P.project_id}' already exists. Use PATCH to update it.")

        # The project does not exist, so any access group already holding this
        # name belongs to something else. add_or_update below would reuse it.
        assert_project_id_available(session, P.project_id)

        if P.project_lead:
            project_lead = resolve_user_by_identifier(session, P.project_lead)
        else:
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
                                          session, role=Role.OWNER,
                                          group_type=AccessGroupType.PROJECT)

        add_or_update(session, Resource,
                      where=(Resource.mfid == prop.unique_id),
                      mfid=prop.unique_id,
                      resource_type="project",
                      created_with_api_version=API_VERSION,
                      api_version_githash=API_VERSION_GITHASH)
        sql_create_resource_acl(ag, prop.unique_id, read=True, write=True,
                                session=session, permission=Role.OWNER)

        if prop not in project_lead.projects:
            project_lead.projects.append(prop)

        session.commit()
        session.refresh(prop)
        capability_principal = (
            auth_res if auth_res.is_platform_admin
            else load_principal(session, auth_res.user_id)
        )
        return ProjectRead(
            **prop.model_dump(),
            capabilities=_project_capabilities(session, capability_principal, prop),
        )


@router.get("/projects", response_model=PaginatedResponse[ProjectWithLead])
def list_projects(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    params: Annotated[ProjectListQuery, Query()],
):
    '''
    Returns a paginated list of projects the user is authorized to see, including the resolved project lead.
    Pass affiliation=owner to return only projects authoritatively owned by the caller.
    '''
    project_id = params.project_id
    accessible_to_user = params.accessible_to_user
    accessible_to_project = params.accessible_to_project
    include_metadata = params.include_metadata
    include_members = params.include_members
    limit = params.limit
    offset = params.offset

    user_unique_id = auth_res.user_id
    if include_metadata:
        loader_opts = [selectinload(Project.resource).selectinload(Resource.scientific_metadata)]
    else:
        loader_opts = [selectinload(Project.resource)]

    with Session(engine) as session:
        has_access_selectors = bool(accessible_to_user or accessible_to_project)
        if has_access_selectors:
            base = select(Project).where(access_selector_filter(
                session,
                auth_res,
                Project,
                user_refs=accessible_to_user,
                project_refs=accessible_to_project,
            ))
            if project_id is not None:
                base = base.where(Project.project_id == project_id)
        elif project_id is not None:
            base = select(Project).where(Project.project_id == project_id)
        elif has_platform_admin_access(auth_res):
            base = select(Project)
        else:
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

        if params.affiliation:
            base = base.where(project_owner_affiliation_filter(auth_res, Project))

        total = session.exec(select(func.count()).select_from(base.subquery())).one()

        props = session.exec(
            base.options(*loader_opts).order_by(col(Project.project_id)).offset(offset).limit(limit)
        ).all()

        orcids = [p.project_lead_orcid for p in props if p.project_lead_orcid]
        leads = {u.unique_id: u for u in session.exec(select(User).where(User.unique_id.in_(orcids))).all()}

        items = []
        for proj in props:
            may_see_private_fields = (
                has_platform_admin_access(auth_res)
                or auth_res.role_in(proj.unique_id) is not None
            )
            lead_user = leads.get(proj.project_lead_orcid)
            lead = UserPublicRead.model_validate(lead_user) if lead_user else None
            resource_md = proj.resource.scientific_metadata if proj.resource and may_see_private_fields else None
            members = _get_project_users(proj.project_id, session) if include_members and may_see_private_fields else None
            items.append(ProjectWithLead(
                **proj.model_dump(),
                lead=lead,
                creation_time=proj.resource.creation_time if proj.resource else None,
                modification_time=proj.resource.modification_time if proj.resource else None,
                scientific_metadata=resource_md.scientific_metadata if include_metadata and resource_md else None,
                members=members,
                capabilities=_project_capabilities(session, auth_res, proj) if project_id is not None else None,
            ))
        return PaginatedResponse(total=total, limit=limit, offset=offset, items=items)


@router.get("/projects/search", response_model=PaginatedResponse[ProjectRead])
def search_projects(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    params: Annotated[SearchQuery, Query()],
):
    '''
    Search projects by title or project_id across all projects, not just ones the
    caller is a member of. This is how a user discovers a project to request to
    join via POST /access_groups/{group_name}/join. Returns results ordered by
    match quality. Maximum 50 results.

    Note: ProjectRead has no lead/scientific_metadata fields, so this is safe to
    open regardless of membership. See GET /projects/{proj_id} for the fields
    that stay member-only.
    '''
    normalized_q, tokens = normalize_search_query(params.q)
    with Session(engine) as session:
        configure_trigram_search(session, get_trgm_search_threshold())
        fields = (Project.project_id, Project.title)
        query = apply_trigram_search(
            select(Project),
            tokens,
            fields=fields,
            prefix_fields=(Project.project_id,),
        )
        if params.affiliation:
            query = query.where(project_owner_affiliation_filter(auth_res, Project))
        query = query.order_by(
            trigram_rank(normalized_q, fields).desc(),
            Project.unique_id.desc(),
        ).limit(params.limit)

        results = session.exec(query).all()
        return PaginatedResponse(limit=params.limit, items=results)


@router.get("/projects/{project_ref}", response_model=ProjectWithLead)
def read_project(project_ref: str, response: Response,
                 auth_res: Annotated[Principal, Depends(authorize_user)],
                 include_metadata: bool = False, include_members: bool = False):
    '''
    Returns the project object for the provided MFID. Project slugs remain
    accepted temporarily for compatibility.
    Any authenticated user can read basic project info (project_id, organization,
    status, title) to support discovery for join requests. Members and admins
    Members and platform administrators additionally get scientific_metadata.
    The lead and member profiles are always public-safe and never include email.
    Non-members never get the member list, regardless of ?include_members=.
    '''
    with Session(engine) as session:
        prop = resolve_project_reference(project_ref, session)
        if prop is None:
            raise HTTPException(status_code=404, detail="Project not found")

        if prop.project_id == project_ref:
            response.headers["Content-Location"] = f"/projects/{prop.unique_id}"

        return load_project_detail(
            session,
            auth_res,
            prop.unique_id,
            project=prop,
            options=ResourceDetailOptions(
                include_metadata=include_metadata,
                include_members=include_members,
            ),
        )


@router.patch("/projects/{project_ref}", response_model=ProjectWithLead)
def update_project(project_ref: str, auth_res: Annotated[Principal, Depends(authorize_user)], project_update: ProjectUpdate):
    '''
    Partially update a project record. Only fields provided in the request body are modified.
    Accessible by admins and the project lead.
    Leadership is not a field here — ProjectUpdate has no project_lead_*
    fields (extra="forbid" 422s if you send one). Use
    POST /resources/{proj_id}/transfer_ownership instead.

    project_id may be changed (renamed) here. Every foreign key that names a
    project stores its MFID (Project.unique_id), never project_id, so a
    rename does not orphan any dataset/sample/ACL row.
    '''
    with Session(engine) as session:
        project = resolve_project_reference(project_ref, session)

        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")

        require_permission(session, auth_res, project.unique_id, Permission.EDIT, legacy_group=project.unique_id)

        update_data = project_update.model_dump(exclude_unset=True)

        if update_data.get("project_id") and update_data["project_id"] != project.project_id:
            existing = session.exec(
                select(Project).where(Project.project_id == update_data["project_id"])
            ).one_or_none()
            if existing is not None:
                raise HTTPException(status_code=409, detail=f"Project '{update_data['project_id']}' already exists.")
            assert_project_id_available(session, update_data["project_id"])

        for key, value in update_data.items():
            setattr(project, key, value)

        session.add(project)
        touch_resource_modification_time(session, project.unique_id)
        session.commit()
        session.refresh(project)
        lead = session.exec(select(User).where(User.unique_id == project.project_lead_orcid)).one_or_none() if project.project_lead_orcid else None
        resource = session.exec(select(Resource).where(Resource.mfid == project.unique_id)).one_or_none()
        return ProjectWithLead(
            **project.model_dump(),
            lead=lead,
            creation_time=resource.creation_time if resource else None,
            modification_time=resource.modification_time if resource else None,
            capabilities=_project_capabilities(session, auth_res, project),
        )


def _get_project_users(proj_id: str, session) -> List[AccessGroupMemberRead]:
    '''Public-safe project members and their role.'''
    return load_project_members(session, proj_id)
@router.get("/projects/{project_ref}/users", response_model=PaginatedResponse[AccessGroupMemberRead])
def read_users_on_project(project_ref: str, auth_res: Annotated[Principal, Depends(authorize_user)],
                          limit: int = Query(default=100, ge=1, le=1000),
                          offset: int = Query(default=0, ge=0)):
    '''
    Returns a paginated list of users associated with the project ID.
    Accessible by admins and members of the project.
    '''
    with Session(engine) as session:
        project = resolve_project_reference(project_ref, session)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")

        require_permission(session, auth_res, project.unique_id, Permission.READ, legacy_group=project.unique_id)

        all_users = _get_project_users(project.project_id, session)
        return PaginatedResponse(total=len(all_users), limit=limit, offset=offset, items=all_users[offset:offset + limit])


@router.post("/projects/{project_ref}/users/{orcid}", response_model=List[AccessGroupMemberRead])
def add_users_to_project(
    project_ref: str,
    orcid: str,
    auth_res: Annotated[Principal, Depends(authorize_user)],
    email: Optional[str] = None,
    username: Optional[str] = None,
    role: ProjectMembershipRole = ProjectMembershipRole.CONTRIBUTOR,
):
    '''
    Add a user to a project at `role`, defaulting to contributor.

    Requires editor or above in the project. Editors may grant contributor or
    viewer, admins may grant editor or below, and owners may grant admin or below.
    Cannot seat someone as owner this way; a project has exactly one owner,
    changed only via POST /resources/{mfid}/transfer_ownership.
    '''
    internal_role = Role.from_db(role.value)
    if internal_role == Role.OWNER:
        raise HTTPException(
            status_code=409,
            detail="Cannot appoint an owner this way; use POST /resources/{mfid}/transfer_ownership")

    with Session(engine) as session:
        proj = resolve_project_reference(project_ref, session)
        if proj is None:
            raise HTTPException(status_code=404, detail=f'Project {project_ref} not found')

        require_permission(session, auth_res, proj.unique_id, Permission.EDIT, legacy_group=proj.unique_id)
        user = resolve_user(session, orcid=orcid if not email and not username else None, email=email, username=username)
        ag = session.exec(
            select(AccessGroup).where(AccessGroup.group_name == proj.unique_id)
        ).one_or_none()
        if ag is not None:
            existing_link = session.exec(
                select(UserAccessGroupLink)
                .where(UserAccessGroupLink.user_id == user.id)
                .where(UserAccessGroupLink.accessgroup_id == ag.id)
            ).one_or_none()
            if existing_link is not None and existing_link.role is not None:
                current_role = Role.from_db(existing_link.role)
                if current_role == internal_role:
                    return _get_project_users(proj.project_id, session)
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"User '{user.unique_id}' is already a member of project "
                        f"'{proj.project_id}' with role '{current_role.db_value}'. "
                        f"Use PATCH /projects/{proj.unique_id}/users/{user.unique_id}"
                        f"?role={internal_role.db_value} to change it."
                    ),
                )

        require_grant(
            session,
            auth_res,
            proj.unique_id,
            internal_role,
            floor=Role.EDITOR,
            allow_peer=False,
        )

        sql_add_user_to_project(user, proj.project_id, session, role=internal_role)
        session.commit()

        return _get_project_users(proj.project_id, session)


@router.patch("/projects/{project_ref}/users/{orcid}", response_model=List[AccessGroupMemberRead])
def set_project_user_role(
    project_ref: str,
    orcid: str,
    auth_res: Annotated[Principal, Depends(authorize_user)],
    role: ProjectMembershipRole,
):
    '''
    Change a member's role in a project.

    Role management follows a strict hierarchy. Editors manage contributor and
    viewer roles, admins manage editor and below, and owners manage admin and below.

    Cannot grant or touch owner standing at all, even as the owner yourself.
    a project has exactly one owner, changed only via
    POST /resources/{mfid}/transfer_ownership.
    '''
    internal_role = Role.from_db(role.value)
    if internal_role == Role.OWNER:
        raise HTTPException(
            status_code=409,
            detail="Cannot appoint an owner this way; use POST /resources/{mfid}/transfer_ownership")

    with Session(engine) as session:
        proj = resolve_project_reference(project_ref, session)
        if proj is None:
            raise HTTPException(status_code=404, detail=f'Project {project_ref} not found')

        require_permission(session, auth_res, proj.unique_id, Permission.EDIT, legacy_group=proj.unique_id)

        user = resolve_user(session, orcid=orcid)
        ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == proj.unique_id)).one_or_none()
        if ag is None:
            raise HTTPException(status_code=404, detail=f'Project {project_ref} not found')

        link = session.exec(
            select(UserAccessGroupLink)
            .where(UserAccessGroupLink.user_id == user.id)
            .where(UserAccessGroupLink.accessgroup_id == ag.id)
        ).one_or_none()
        if link is None:
            raise HTTPException(status_code=404,
                                detail=f'{orcid} is not a member of {proj.project_id}')

        current = Role.from_db(link.role) if link.role else None
        if current is Role.OWNER:
            raise HTTPException(
                status_code=409,
                detail="Cannot change the owner's standing this way; use POST /resources/{mfid}/transfer_ownership")
        require_grant(
            session,
            auth_res,
            proj.unique_id,
            internal_role,
            current=current,
            floor=Role.EDITOR,
            allow_peer=False,
        )

        link.role = internal_role.db_value
        session.add(link)
        touch_resource_modification_time(session, proj.unique_id)
        session.commit()

        return _get_project_users(proj.project_id, session)


@router.delete("/projects/{project_ref}/users/{orcid}", response_model=List[AccessGroupMemberRead])
def remove_user_from_project(project_ref: str, orcid: str, auth_res: Annotated[Principal, Depends(authorize_user)],
                             email: Optional[str] = None, username: Optional[str] = None):
    '''
    Remove a user from a project and its associated access group.
    Admins or the project lead can remove any member. A member can also
    remove themselves (leave the project), but the project lead cannot
    remove themselves — transfer leadership first.
    Provide ?email= or ?username= to look up the user instead of the orcid path parameter.
    '''
    user_unique_id = auth_res.user_id
    is_admin = has_platform_admin_access(auth_res)

    with Session(engine) as session:
        proj = resolve_project_reference(project_ref, session)
        if proj is None:
            raise HTTPException(status_code=404, detail=f"Project {project_ref} not found")

        is_self = user_unique_id == orcid and not email and not username
        caller_is_owner = is_project_owner(user_unique_id, proj.project_id, session)
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
            raise HTTPException(status_code=404, detail=f"User {user.unique_id} is not a member of project {proj.project_id}")
        session.delete(ag_link)

        # Best-effort cleanup of the legacy link table; its absence is not an error.
        link = session.exec(
            select(UserProjectLink).where(
                UserProjectLink.user_id == user.id,
                UserProjectLink.project_id == proj.project_id
            )
        ).one_or_none()
        if link is not None:
            session.delete(link)

        touch_resource_modification_time(session, proj.unique_id)
        session.commit()
        return _get_project_users(proj.project_id, session)


@router.get("/projects/{project_ref}/sample_graph")
def project_sample_graph(project_ref: str, auth_res: Annotated[Principal, Depends(authorize_user)]):

    with Session(engine) as session:
        proj = resolve_project_reference(project_ref, session)
        if proj is None:
            raise HTTPException(status_code=404, detail="Project not found")
        require_permission(session, auth_res, proj.unique_id, Permission.READ, legacy_group=proj.unique_id)

        samp_cmd = (
            select(Sample)
            .options(joinedload(Sample.parents))
            .where(permission_filter(auth_res, Sample, Permission.READ))
            .where(deletion_filter_for(Sample))
        )

        # Filter samples by the project's MFID, not its (renamable) project_id
        samp_cmd = samp_cmd.where(Sample.project_mfid == proj.unique_id)
        samp_list = session.exec(samp_cmd).unique().all()

        allowed_ids = {sample.unique_id for sample in samp_list}
        edges = []
        for sample in samp_list:
            for parent in sample.parents:
                if parent.unique_id in allowed_ids:
                    edges.append((parent.unique_id, sample.unique_id))

        G = nx.DiGraph(edges)
        for sample in samp_list:
            G.add_node(sample.unique_id,
                       sample_name=sample.sample_name)

        return json_graph.node_link_data(G)

@router.get("/projects/{project_ref}/dataset_graph")
def project_dataset_graph(project_ref: str, auth_res: Annotated[Principal, Depends(authorize_user)]):

    with Session(engine) as session:
        proj = resolve_project_reference(project_ref, session)
        if proj is None:
            raise HTTPException(status_code=404, detail="Project not found")

        ds_cmd = (
            select(Dataset)
            .options(joinedload(Dataset.parents))
            .filter(permission_filter(auth_res, Dataset, Permission.READ))
            .where(deletion_filter_for(Dataset))
            .where(Dataset.project_mfid == proj.unique_id)
        )

        ds_list = session.exec(ds_cmd).unique().all()

        allowed_ids = {dataset.unique_id for dataset in ds_list}
        edges = []
        for dataset in ds_list:
            for parent in dataset.parents:
                if parent.unique_id in allowed_ids:
                    edges.append((parent.unique_id, dataset.unique_id))

        G = nx.DiGraph(edges)
        for dataset in ds_list:
            G.add_node(dataset.unique_id,
                       dataset_name=dataset.dataset_name)

        return json_graph.node_link_data(G)


@router.get("/projects/{project_ref}/entity_graph")
def project_entity_graph(project_ref: str, auth_res: Annotated[Principal, Depends(authorize_user)]):
    '''
    Returns a NetworkX node-link graph of all samples and datasets in the
    project, connected via sample lineage, dataset lineage, and sample-dataset
    associations. Nodes carry entity_type ('sample' or 'dataset') and name.
    '''
    with Session(engine) as session:
        proj = resolve_project_reference(project_ref, session)
        if proj is None:
            raise HTTPException(status_code=404, detail="Project not found")
        require_permission(session, auth_res, proj.unique_id, Permission.READ, legacy_group=proj.unique_id)

        # Load all samples in the project with parent relationships. Filtered
        # by the project's MFID, not its (renamable) project_id.
        proj_mfid = proj.unique_id
        samp_cmd = (
            select(Sample)
            .options(joinedload(Sample.parents))
            .where(permission_filter(auth_res, Sample, Permission.READ))
            .where(deletion_filter_for(Sample))
            .where(Sample.project_mfid == proj_mfid)
        )
        samp_list = session.exec(samp_cmd).unique().all()

        # Load all authorized datasets in the project with parent relationships
        ds_list = session.exec(
            select(Dataset)
            .options(joinedload(Dataset.parents))
            .filter(permission_filter(auth_res, Dataset, Permission.READ))
            .where(deletion_filter_for(Dataset))
            .where(Dataset.project_mfid == proj_mfid)
        ).unique().all()

        s_int_to_uid = {s.id: s.unique_id for s in samp_list}
        d_int_to_uid = {d.id: d.unique_id for d in ds_list}
        sample_mfids = set(s_int_to_uid.values())
        dataset_mfids = set(d_int_to_uid.values())

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
                if parent.unique_id in sample_mfids:
                    G.add_edge(parent.unique_id, s.unique_id)
        for d in ds_list:
            G.add_node(d.unique_id, entity_type='dataset', name=d.dataset_name)
            for parent in d.parents:
                if parent.unique_id in dataset_mfids:
                    G.add_edge(parent.unique_id, d.unique_id)
        for link in sd_links:
            G.add_edge(s_int_to_uid[link.sample_id], d_int_to_uid[link.dataset_id])

        return json_graph.node_link_data(G)

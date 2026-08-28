from typing import Annotated, List, Optional, Union

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import desc, func, join, text, Text
from sqlalchemy.orm import attributes, selectinload
from sqlalchemy.sql import and_, exists
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.general_utils import get_utc_isoformat
from app.models.core_models import (
    AccessGroup,
    Dataset, DatasetLink, DatasetSampleLink,
    DeletionRequest,
    Instrument, Project, Resource,
    ResourceACL, Sample, SampleLink,
    ScientificMetadataBinary, User, UserAccessGroupLink,
)
from app.models.access import AccessGrantRead, AccessGrantWrite
from app.models.access_groups import AccessGroupRead
from app.models.datasets import DatasetRead, DatasetResponse, ScientificMetadataBinaryRead
from app.models.deletion import DeletionRequestRead
from app.models.instrument import InstrumentResponse
from app.models.links import LinkedResource
from app.models.projects import ProjectWithLead
from app.models.resource import (
    ReassignProjectRead, ReassignProjectRequest, TransferOwnershipRead, TransferOwnershipRequest)
from app.models.samples import SampleResponse
from app.models.users import UserPublicRead
from app.models.search import ResourceSearchResult
from app.models.pagination import PaginatedResponse
from app.utils.auth_utils import (
    authorize_user, require_grant, permission_filter, require_permission, require_platform_admin)
from app.utils.permissions import (
    PUBLIC_GROUP_NAME, AccessGroupType, Permission, Principal, Role, can_grant, effective_role)
from app.utils.sql_utils import add_or_update, deletion_filter_for, get_active_deletion_request, set_public, sql_create_resource_acl, touch_resource_modification_time
from app.utils.gcs_record_utils import write_metadata
from app.utils.instrument_utils import resolve_instrument_mfids_to_ids
from app.utils.project_utils import is_project_group, resolve_project_mfids_to_ids
from app.utils.user_utils import resolve_user_by_identifier


router = APIRouter(tags=["resources"])
engine = get_engine()


def _fetch_links(session, resource_type: str, entity) -> List[LinkedResource]:
    results: List[LinkedResource] = []
    ds_del = deletion_filter_for(Dataset)
    s_del = deletion_filter_for(Sample)

    if resource_type == "dataset":
        parents = session.exec(
            select(Dataset).join(DatasetLink, DatasetLink.parent_dataset_id == Dataset.id)
            .where(DatasetLink.child_dataset_id == entity.id)
            .where(ds_del)
        ).all()
        results += [LinkedResource(unique_id=d.unique_id, resource_type="dataset", name=d.dataset_name, relationship="parent") for d in parents]

        children = session.exec(
            select(Dataset).join(DatasetLink, DatasetLink.child_dataset_id == Dataset.id)
            .where(DatasetLink.parent_dataset_id == entity.id)
            .where(ds_del)
        ).all()
        results += [LinkedResource(unique_id=d.unique_id, resource_type="dataset", name=d.dataset_name, relationship="child") for d in children]

        samples = session.exec(
            select(Sample).join(DatasetSampleLink, DatasetSampleLink.sample_id == Sample.id)
            .where(DatasetSampleLink.dataset_id == entity.id)
            .where(s_del)
        ).all()
        results += [LinkedResource(unique_id=s.unique_id, resource_type="sample", name=s.sample_name, relationship="associated") for s in samples]

    elif resource_type == "sample":
        parents = session.exec(
            select(Sample).join(SampleLink, SampleLink.parent_sample_id == Sample.id)
            .where(SampleLink.sample_id == entity.id)
            .where(s_del)
        ).all()
        results += [LinkedResource(unique_id=s.unique_id, resource_type="sample", name=s.sample_name, relationship="parent") for s in parents]

        children = session.exec(
            select(Sample).join(SampleLink, SampleLink.sample_id == Sample.id)
            .where(SampleLink.parent_sample_id == entity.id)
            .where(s_del)
        ).all()
        results += [LinkedResource(unique_id=s.unique_id, resource_type="sample", name=s.sample_name, relationship="child") for s in children]

        datasets = session.exec(
            select(Dataset).join(DatasetSampleLink, DatasetSampleLink.dataset_id == Dataset.id)
            .where(DatasetSampleLink.sample_id == entity.id)
            .where(ds_del)
        ).all()
        results += [LinkedResource(unique_id=d.unique_id, resource_type="dataset", name=d.dataset_name, relationship="associated") for d in datasets]

    return results


@router.get("/resources/metadata/search", response_model=PaginatedResponse[ResourceSearchResult])
def search_resource_metadata_v2(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    q: str = Query(min_length=3, description="Full-text search against scientific metadata content."),
    limit: int = Query(default=20, ge=1, le=50),
):
    """
    Search resources by scientific metadata content using full-text search.
    Returns a unified structure compatible with other search endpoints, including scientific_metadata.
    """

    search_query = func.plainto_tsquery('english', q)
    metadata_vector = func.to_tsvector(
        'english',
        func.jsonb_path_query_array(ScientificMetadataBinary.scientific_metadata, text("'$[*]'")).cast(Text)
    )
    rank_expr = func.ts_rank_cd(metadata_vector, search_query)

    auth_filter = permission_filter(auth_res, ScientificMetadataBinary, Permission.READ)

    active_deletion = exists().where(
        DeletionRequest.resource_id == ScientificMetadataBinary.unique_id,
        DeletionRequest.status.in_(["pending", "approved"])
    )

    with Session(engine) as session:
        rows = session.exec(
            select(ScientificMetadataBinary, Resource, rank_expr.label("rank"))
            .join(Resource, Resource.mfid == ScientificMetadataBinary.unique_id)
            .where(and_(
                auth_filter,
                metadata_vector.op('@@')(search_query),
                ~active_deletion,
            ))
            .order_by(desc("rank"))
            .limit(limit)
        ).all()

        if not rows:
            return PaginatedResponse(limit=limit, items=[])

        # Group mfids by resource type for batch name resolution
        by_type: dict = {}
        for smb, resource, rank in rows:
            by_type.setdefault(resource.resource_type, []).append(smb.unique_id)

        name_map: dict = {}
        owner_map: dict = {}
        if "dataset" in by_type:
            for ds in session.exec(select(Dataset).where(Dataset.unique_id.in_(by_type["dataset"]))).all():
                name_map[ds.unique_id] = ds.dataset_name
                owner_map[ds.unique_id] = ds.owner_orcid
        if "sample" in by_type:
            for s in session.exec(select(Sample).where(Sample.unique_id.in_(by_type["sample"]))).all():
                name_map[s.unique_id] = s.sample_name
                owner_map[s.unique_id] = s.owner_orcid
        if "instrument" in by_type:
            for i in session.exec(select(Instrument).where(Instrument.unique_id.in_(by_type["instrument"]))).all():
                name_map[i.unique_id] = i.instrument_name
        if "project" in by_type:
            for p in session.exec(select(Project).where(Project.project_id.in_(by_type["project"]))).all():
                name_map[p.project_id] = p.title

        items = [
            ResourceSearchResult(
                unique_id=resource.mfid,
                resource_type=resource.resource_type,
                name=name_map.get(resource.mfid),
                owner_orcid=owner_map.get(resource.mfid),
                creation_time=resource.creation_time,
                modification_time=resource.modification_time,
                rank=float(rank) if rank is not None else None,
                scientific_metadata=smb.scientific_metadata,
            )
            for smb, resource, rank in rows
        ]

    return PaginatedResponse(limit=limit, items=items)


@router.get("/resources/{resource_id}", response_model=Union[DatasetResponse, SampleResponse, InstrumentResponse, ProjectWithLead])
def get_resource(auth_res: Annotated[Principal, Depends(authorize_user)], resource_id: str,
                 include_metadata: bool = False, include_links: bool = False,
                 include_owner: bool = False):
    """
    Fetch a dataset, sample, instrument, or project by ID without knowing its type in advance.
    Resolves the type from idtype, then returns the full resource record.
    Pass include_metadata=true to include scientific metadata.
    Pass include_links=true to include parent/child/associated resources (datasets and samples only).
    """

    with Session(engine) as session:
        resource = session.exec(
            select(Resource).where(Resource.mfid == resource_id)
        ).one_or_none()
        if resource is None:
            raise HTTPException(status_code=404, detail="Resource not found")

        require_permission(session, auth_res, resource_id, Permission.READ)

        deletion_req = get_active_deletion_request(session, resource_id)

        sci_md = None
        if include_metadata:
            smb = session.exec(
                select(ScientificMetadataBinary).where(ScientificMetadataBinary.unique_id == resource_id)
            ).one_or_none()
            sci_md = smb.scientific_metadata if smb else None

        def _resolve_owner(orcid):
            if not include_owner or not orcid:
                return None
            u = session.exec(select(User).where(User.unique_id == orcid)).one_or_none()
            return UserPublicRead.model_validate(u) if u else None

        if resource.resource_type == "dataset":
            ds = session.exec(
                select(Dataset).where(Dataset.unique_id == resource_id)
            ).one_or_none()
            if ds is None:
                raise HTTPException(status_code=404, detail="Dataset not found")
            instrument_ids = resolve_instrument_mfids_to_ids([ds.instrument_mfid], session)
            project_ids = resolve_project_mfids_to_ids([ds.project_mfid], session)
            return DatasetResponse.model_validate({
                **ds.model_dump(),
                "instrument_id": instrument_ids.get(ds.instrument_mfid),
                "project_id": project_ids.get(ds.project_mfid, ds.project_id),
                "public": resource.public,
                "creation_time": resource.creation_time,
                "modification_time": resource.modification_time,
                "scientific_metadata": sci_md,
                "deletion_request": deletion_req.model_dump() if deletion_req else None,
                "links": _fetch_links(session, "dataset", ds) if include_links else None,
                "owner": _resolve_owner(ds.owner_orcid),
            })

        elif resource.resource_type == "sample":
            samp = session.exec(
                select(Sample)
                .options(selectinload(Sample.datasets))
                .where(Sample.unique_id == resource_id)
            ).one_or_none()
            if samp is None:
                raise HTTPException(status_code=404, detail="Sample not found")
            instrument_ids = resolve_instrument_mfids_to_ids(
                [ds.instrument_mfid for ds in samp.datasets], session)
            ds_project_ids = resolve_project_mfids_to_ids(
                [ds.project_mfid for ds in samp.datasets] + [samp.project_mfid], session)
            return SampleResponse.model_validate({
                **samp.model_dump(),
                "project_id": ds_project_ids.get(samp.project_mfid, samp.project_id),
                "public": resource.public,
                "datasets": [DatasetRead.model_validate({
                    **ds.model_dump(),
                    "instrument_id": instrument_ids.get(ds.instrument_mfid),
                    "project_id": ds_project_ids.get(ds.project_mfid, ds.project_id),
                }) for ds in samp.datasets],
                "creation_time": resource.creation_time,
                "modification_time": resource.modification_time,
                "scientific_metadata": sci_md,
                "deletion_request": deletion_req.model_dump() if deletion_req else None,
                "links": _fetch_links(session, "sample", samp) if include_links else None,
                "owner": _resolve_owner(samp.owner_orcid),
            })

        elif resource.resource_type == "instrument":
            inst = session.exec(
                select(Instrument).where(Instrument.unique_id == resource_id)
            ).one_or_none()
            if inst is None:
                raise HTTPException(status_code=404, detail="Instrument not found")
            return InstrumentResponse.model_validate({
                **inst.model_dump(),
                "creation_time": resource.creation_time,
                "modification_time": resource.modification_time,
                "scientific_metadata": sci_md,
            })

        elif resource.resource_type == "project":
            proj = session.exec(
                select(Project).where(Project.unique_id == resource_id)
            ).one_or_none()
            if proj is None:
                raise HTTPException(status_code=404, detail="Project not found")
            lead = None
            if proj.project_lead_orcid:
                lead = session.exec(
                    select(User).where(User.unique_id == proj.project_lead_orcid)
                ).one_or_none()
            return ProjectWithLead(
                **proj.model_dump(),
                lead=lead,
                scientific_metadata=sci_md,
                creation_time=resource.creation_time,
                modification_time=resource.modification_time,
            )

        else:
            raise HTTPException(status_code=404, detail="Resource not found")


@router.get("/resources/{resource_id}/links", response_model=List[LinkedResource])
def get_resource_links(resource_id: str, auth_res: Annotated[Principal, Depends(authorize_user)]):
    """
    Returns the immediate links for a resource (dataset or sample):
    parent/child via lineage and associated resources.
    """

    with Session(engine) as session:
        resource = session.exec(
            select(Resource).where(Resource.mfid == resource_id)
        ).one_or_none()
        if resource is None:
            raise HTTPException(status_code=404, detail="Resource not found")

        require_permission(session, auth_res, resource_id, Permission.READ)

        if resource.resource_type == "dataset":
            entity = session.exec(select(Dataset).where(Dataset.unique_id == resource_id)).one_or_none()
            if entity is None:
                raise HTTPException(status_code=404, detail="Dataset not found")
        elif resource.resource_type == "sample":
            entity = session.exec(select(Sample).where(Sample.unique_id == resource_id)).one_or_none()
            if entity is None:
                raise HTTPException(status_code=404, detail="Sample not found")
        else:
            raise HTTPException(status_code=400, detail=f"Links not supported for resource type '{resource.resource_type}'")

        return _fetch_links(session, resource.resource_type, entity)


@router.get("/resources/{unique_id}/metadata", response_model=ScientificMetadataBinaryRead)
def read_resource_metadata(auth_res: Annotated[Principal, Depends(authorize_user)], unique_id: str):
    with Session(engine) as session:
        resource = session.exec(
            select(Resource)
            .options(selectinload(Resource.scientific_metadata))
            .where(Resource.mfid == unique_id)
        ).one_or_none()

        if resource is None:
            raise HTTPException(status_code=404, detail="Resource not found")

        require_permission(session, auth_res, unique_id, Permission.READ)

        if resource.scientific_metadata is None:
            return ScientificMetadataBinaryRead(unique_id=unique_id, scientific_metadata={})
        return resource.scientific_metadata


@router.post("/resources/{unique_id}/metadata", response_model=ScientificMetadataBinaryRead)
def create_resource_metadata(auth_res: Annotated[Principal, Depends(authorize_user)], unique_id: str, scientific_md: dict,
                             background_tasks: BackgroundTasks, overwrite: bool = False):
    user_unique_id = auth_res.user_id

    with Session(engine) as session:
        resource = session.exec(
            select(Resource).where(Resource.mfid == unique_id)
        ).one_or_none()

        if resource is None:
            raise HTTPException(status_code=404, detail="Resource not found")

        require_permission(session, auth_res, unique_id, Permission.EDIT)

        if not overwrite:
            existing = session.exec(
                select(ScientificMetadataBinary).where(ScientificMetadataBinary.unique_id == unique_id)
            ).one_or_none()
            if existing and existing.scientific_metadata:
                raise HTTPException(
                    status_code=409,
                    detail="Metadata already exists. Use overwrite=true to replace it, or PATCH to merge."
                )

        new_md_obj = add_or_update(
            session,
            ScientificMetadataBinary,
            where=(ScientificMetadataBinary.unique_id == unique_id),
            unique_id=unique_id,
            scientific_metadata=scientific_md,
        )
        attributes.flag_modified(new_md_obj, 'scientific_metadata')
        resource.modification_time = get_utc_isoformat()

        session.commit()
        session.refresh(new_md_obj)
        background_tasks.add_task(write_metadata, unique_id, scientific_md, user_unique_id, "create")
        return new_md_obj


@router.patch("/resources/{unique_id}/metadata", response_model=ScientificMetadataBinaryRead)
def update_resource_metadata(auth_res: Annotated[Principal, Depends(authorize_user)], unique_id: str, scientific_md_updates: dict,
                             background_tasks: BackgroundTasks):
    user_unique_id = auth_res.user_id

    with Session(engine) as session:
        resource = session.exec(
            select(Resource).where(Resource.mfid == unique_id)
        ).one_or_none()

        if resource is None:
            raise HTTPException(status_code=404, detail="Resource not found")

        require_permission(session, auth_res, unique_id, Permission.EDIT)

        stored_md = session.exec(
            select(ScientificMetadataBinary).where(ScientificMetadataBinary.unique_id == unique_id)
        ).one_or_none()
        md_dict = {**(stored_md.scientific_metadata if stored_md else {}), **scientific_md_updates}

        new_md_obj = add_or_update(
            session,
            ScientificMetadataBinary,
            where=(ScientificMetadataBinary.unique_id == unique_id),
            unique_id=unique_id,
            scientific_metadata=md_dict,
        )
        attributes.flag_modified(new_md_obj, 'scientific_metadata')
        resource.modification_time = get_utc_isoformat()

        session.commit()
        session.refresh(new_md_obj)
        background_tasks.add_task(write_metadata, unique_id, md_dict, user_unique_id, "update")
        return new_md_obj


@router.post("/resources/{resource_id}/delete", response_model=DeletionRequestRead)
def create_deletion_request(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    resource_id: str,
    reason: Optional[str] = None,
):
    '''
    Request deletion of a resource. Any authenticated user with access to the resource may submit.
    Returns 409 if an active deletion request already exists for this resource.
    '''
    requester_id = auth_res.user_id
    with Session(engine) as session:
        resource = session.exec(
            select(Resource).where(Resource.mfid == resource_id)
        ).one_or_none()
        if resource is None:
            raise HTTPException(status_code=404, detail=f"Resource {resource_id} not found.")

        if resource.resource_type not in ("dataset", "sample"):
            raise HTTPException(
                status_code=400,
                detail=f"Deletion requests are not supported for resource type '{resource.resource_type}'."
            )

        existing = session.exec(
            select(DeletionRequest)
            .where(DeletionRequest.resource_id == resource_id)
            .where(DeletionRequest.status.in_(["pending", "approved"]))
        ).one_or_none()
        if existing:
            raise HTTPException(
                status_code=409,
                detail=f"A deletion request for {resource_id} already exists (status: {existing.status}).",
            )

        resource_name = None
        if resource.resource_type == "dataset":
            ds = session.exec(select(Dataset).where(Dataset.unique_id == resource_id)).one_or_none()
            resource_name = ds.dataset_name if ds else None
        elif resource.resource_type == "sample":
            samp = session.exec(select(Sample).where(Sample.unique_id == resource_id)).one_or_none()
            resource_name = samp.sample_name if samp else None

        deletion_req = DeletionRequest(
            resource_id=resource_id,
            resource_name=resource_name,
            requester_id=requester_id,
            reason=reason,
            status="pending",
        )
        session.add(deletion_req)
        touch_resource_modification_time(session, resource_id)
        session.commit()
        session.refresh(deletion_req)
        return DeletionRequestRead(
            **deletion_req.model_dump(),
            resource_type=deletion_req.resource.resource_type if deletion_req.resource else None,
        )


def _set_resource_access_grant(
    session: Session,
    auth_res: Principal,
    mfid: str,
    access_group: AccessGroup,
    target: Role,
) -> ResourceACL:
    require_permission(session, auth_res, mfid, Permission.MANAGE_ACL)
    existing = session.exec(
        select(ResourceACL)
        .where(ResourceACL.mfid == mfid)
        .where(ResourceACL.accessgroup_id == access_group.id)
    ).one_or_none()
    current = Role.from_db(existing.permission) if existing and existing.permission else None
    if target is Role.OWNER:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot grant owner access this way; use POST /resources/{mfid}/transfer_ownership",
        )
    if current is Role.OWNER:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot change the owner's access this way; use POST /resources/{mfid}/transfer_ownership",
        )

    require_grant(session, auth_res, mfid, target, current=current)
    if existing is None:
        existing = ResourceACL(
            mfid=mfid,
            accessgroup_id=access_group.id,
            read=True,
            write=target >= Role.EDITOR,
            permission=target.db_value,
        )
    else:
        existing.read = True
        existing.write = target >= Role.EDITOR
        existing.permission = target.db_value
    session.add(existing)
    touch_resource_modification_time(session, mfid)
    return existing


@router.get(
    "/resources/{mfid}/access_groups",
    response_model=List[AccessGroupRead],
    deprecated=True,
)
def read_resource_acl(mfid: str, auth_res: Annotated[Principal, Depends(authorize_user)]):
    require_platform_admin(auth_res)
    with Session(engine) as session:
        # Verify resource exists
        resource = session.exec(select(Resource).where(Resource.mfid == mfid)).one_or_none()

        if resource is None:
            raise HTTPException(status_code=404, detail="Resource not found")

        cmd = select(AccessGroup).select_from(
            join(AccessGroup,
                 join(ResourceACL, Resource, ResourceACL.mfid == Resource.mfid),
                 AccessGroup.id == ResourceACL.accessgroup_id)
        ).where(Resource.mfid == mfid)
        acl = session.exec(cmd).all()

        return acl


@router.post(
    "/resources/{mfid}/access_groups",
    response_model=ResourceACL,
    deprecated=True,
)
def create_resource_acl(group_name: str, mfid: str, read: bool, write: bool, auth_res: Annotated[Principal, Depends(authorize_user)]):
    require_platform_admin(auth_res)
    if not read:
        raise HTTPException(status_code=422, detail="read must be true; use the revoke route to remove access")
    permission = Role.EDITOR if write else Role.VIEWER
    with Session(engine) as session:
        resource = session.exec(select(Resource).where(Resource.mfid == mfid)).one_or_none()
        if resource is None:
            raise HTTPException(status_code=404, detail="Resource not found")
        ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == group_name)).one_or_none()
        if ag is None:
            raise HTTPException(status_code=404, detail="Access Group not found")
        acl = _set_resource_access_grant(session, auth_res, mfid, ag, permission)
        session.commit()
        session.refresh(acl)
    return acl


# ---------------------------------------------------------------------------
# Resource access grants
#
# Replaces the /access_groups pair, which could grant but never revoke, showed
# group names without their roles, and required callers to know the internal
# group naming — which is due to change to MFIDs. Those routes are kept.
#
# Principals are addressed as a caller thinks of them (a person, a project,
# `public`), typed by path segment so that a username cannot be mistaken for a
# project id.
# ---------------------------------------------------------------------------

def _access_grant_read(session, access_group: AccessGroup, permission: str) -> AccessGrantRead:
    group_name = access_group.group_name
    group_type = access_group.group_type
    if group_type == AccessGroupType.SYSTEM.value and group_name == PUBLIC_GROUP_NAME:
        return AccessGrantRead(
            principal_id=PUBLIC_GROUP_NAME,
            principal_type="public",
            permission=permission,
            display_name="All authenticated users",
        )
    if group_type == AccessGroupType.SYSTEM.value:
        return AccessGrantRead(
            principal_id=group_name,
            principal_type="system",
            permission=permission,
            display_name=group_name,
        )
    if group_type == AccessGroupType.IDENTITY.value:
        user = session.exec(select(User).where(User.unique_id == group_name)).one_or_none()
    else:
        user = None
    if user:
        display_name = f"{user.first_name} {user.last_name}".strip() or None
        return AccessGrantRead(
            principal_id=user.unique_id,
            principal_type="service_account" if user.is_service_account else "user",
            permission=permission,
            slug=user.username,
            display_name=display_name,
        )
    if group_type == AccessGroupType.PROJECT.value:
        project = session.exec(select(Project).where(Project.unique_id == group_name)).one_or_none()
    else:
        project = None
    if project:
        return AccessGrantRead(
            principal_id=project.unique_id,
            principal_type="project",
            permission=permission,
            slug=project.project_id,
            display_name=project.title or project.project_id,
        )
    if group_type == AccessGroupType.INSTRUMENT.value:
        instrument = session.exec(select(Instrument).where(Instrument.unique_id == group_name)).one_or_none()
    else:
        instrument = None
    if instrument:
        return AccessGrantRead(
            principal_id=instrument.unique_id,
            principal_type="instrument",
            permission=permission,
            slug=instrument.instrument_id,
            display_name=instrument.instrument_name,
        )
    return AccessGrantRead(
        principal_id=group_name,
        principal_type="unknown",
        permission=permission,
    )


def _resolve_grantee(session, kind: str, principal: str):
    """(AccessGroup, canonical principal) for a typed path segment."""
    if kind == "public":
        ag = session.exec(select(AccessGroup).where(
            AccessGroup.group_name == PUBLIC_GROUP_NAME,
            AccessGroup.group_type == AccessGroupType.SYSTEM.value,
        )).one_or_none()
        if ag is None:
            raise HTTPException(status_code=409,
                                detail="No public access group — has the migration run?")
        return ag, PUBLIC_GROUP_NAME

    if kind == "users":
        # Also accepts email now, via the same format-sniffing transfer_ownership uses.
        user = resolve_user_by_identifier(session, principal)
        group_name = user.unique_id
        group_type = AccessGroupType.IDENTITY.value
    elif kind == "projects":
        proj = session.exec(select(Project).where(
            Project.project_id == principal)).one_or_none()
        if proj is None:
            raise HTTPException(status_code=404, detail=f"Project '{principal}' not found")
        group_name = proj.unique_id
        group_type = AccessGroupType.PROJECT.value
    else:
        raise HTTPException(status_code=404, detail=f"Unknown principal kind '{kind}'")

    ag = session.exec(select(AccessGroup).where(
        AccessGroup.group_name == group_name,
        AccessGroup.group_type == group_type,
    )).one_or_none()
    if ag is None:
        raise HTTPException(status_code=404,
                            detail=f"No access group for '{principal}'")
    return ag, group_name


@router.get("/resources/{mfid}/access", response_model=List[AccessGrantRead])
def list_resource_access(mfid: str, auth_res: Annotated[Principal, Depends(authorize_user)]):
    """List the grants on a resource. Requires permission to manage its ACL."""
    with Session(engine) as session:
        resource = session.exec(select(Resource).where(Resource.mfid == mfid)).one_or_none()
        if resource is None:
            raise HTTPException(status_code=404, detail="Resource not found")
        require_permission(session, auth_res, mfid, Permission.MANAGE_ACL)

        rows = session.exec(
            select(ResourceACL, AccessGroup)
            .join(AccessGroup, AccessGroup.id == ResourceACL.accessgroup_id)
            .where(ResourceACL.mfid == mfid)
        ).all()

        return [_access_grant_read(session, ag, acl.permission) for acl, ag in rows]


@router.put("/resources/{mfid}/access/{kind}/{principal}", response_model=AccessGrantRead)
def set_resource_access(mfid: str, kind: str, principal: str, body: AccessGrantWrite,
                        auth_res: Annotated[Principal, Depends(authorize_user)]):
    """Grant or change a principal's access. Idempotent.

    `kind` is `users` or `projects`. Requires admin on the resource, and you
    may not grant above your own role nor overwrite a grant above it. Owner
    grants and changes to an existing owner require transfer_ownership.
    """
    if kind not in ("users", "projects"):
        raise HTTPException(status_code=404,
                            detail="kind must be 'users' or 'projects'; use /access/public for public")
    try:
        target = Role.from_db(body.permission.value)
    except KeyError:
        raise HTTPException(status_code=422,
                            detail=f"permission must be one of {[r.db_value for r in Role]}")
    with Session(engine) as session:
        resource = session.exec(select(Resource).where(Resource.mfid == mfid)).one_or_none()
        if resource is None:
            raise HTTPException(status_code=404, detail="Resource not found")
        require_permission(session, auth_res, mfid, Permission.MANAGE_ACL)

        if target is Role.OWNER:
            raise HTTPException(
                status_code=409,
                detail=f"Cannot grant owner access this way; use POST /resources/{mfid}/transfer_ownership")

        ag, canonical = _resolve_grantee(session, kind, principal)
        _set_resource_access_grant(session, auth_res, mfid, ag, target)
        session.commit()

        return _access_grant_read(session, ag, target.db_value)


@router.delete("/resources/{mfid}/access/{kind}/{principal}")
def revoke_resource_access(mfid: str, kind: str, principal: str,
                           auth_res: Annotated[Principal, Depends(authorize_user)]):
    """Revoke a principal's access.

    Refuses to remove an `owner` grant: that would leave the resource with
    nobody able to transfer it or manage its ACL again. Use transfer_ownership.
    """
    if kind not in ("users", "projects"):
        raise HTTPException(status_code=404, detail="kind must be 'users' or 'projects'")

    with Session(engine) as session:
        resource = session.exec(select(Resource).where(Resource.mfid == mfid)).one_or_none()
        if resource is None:
            raise HTTPException(status_code=404, detail="Resource not found")
        require_permission(session, auth_res, mfid, Permission.MANAGE_ACL)

        ag, canonical = _resolve_grantee(session, kind, principal)
        existing = session.exec(
            select(ResourceACL).where(ResourceACL.mfid == mfid)
            .where(ResourceACL.accessgroup_id == ag.id)).one_or_none()
        if existing is None:
            raise HTTPException(status_code=404,
                                detail=f"'{canonical}' has no access to this resource")

        current = Role.from_db(existing.permission) if existing.permission else None
        if current is Role.OWNER:
            raise HTTPException(
                status_code=409,
                detail="Cannot revoke the owner's access; transfer ownership instead")

        require_grant(session, auth_res, mfid, current or Role.VIEWER, current=current)

        session.delete(existing)
        touch_resource_modification_time(session, mfid)
        session.commit()
    return {"detail": f"Access revoked for {canonical}"}


@router.put("/resources/{mfid}/access/public", response_model=AccessGrantRead)
def publish_resource(mfid: str, auth_res: Annotated[Principal, Depends(authorize_user)]):
    """Make a resource publicly readable.

    No role parameter: standing in the public group is viewer, so min() caps
    any grant there. A world-writable resource is unrepresentable, and offering
    a role would invent an error case the model does not have.
    """
    with Session(engine) as session:
        resource = session.exec(select(Resource).where(Resource.mfid == mfid)).one_or_none()
        if resource is None:
            raise HTTPException(status_code=404, detail="Resource not found")
        require_permission(session, auth_res, mfid, Permission.MANAGE_ACL)
        require_grant(session, auth_res, mfid, Role.VIEWER)

        set_public(session, mfid, True)
        resource.public = True
        session.add(resource)
        touch_resource_modification_time(session, mfid)
        session.commit()

        ag, _ = _resolve_grantee(session, "public", PUBLIC_GROUP_NAME)
        return _access_grant_read(session, ag, Role.VIEWER.db_value)


@router.delete("/resources/{mfid}/access/public")
def unpublish_resource(mfid: str, auth_res: Annotated[Principal, Depends(authorize_user)]):
    """Withdraw public access."""
    with Session(engine) as session:
        resource = session.exec(select(Resource).where(Resource.mfid == mfid)).one_or_none()
        if resource is None:
            raise HTTPException(status_code=404, detail="Resource not found")
        require_permission(session, auth_res, mfid, Permission.MANAGE_ACL)
        require_grant(session, auth_res, mfid, Role.VIEWER)

        set_public(session, mfid, False)
        resource.public = False
        session.add(resource)
        touch_resource_modification_time(session, mfid)
        session.commit()
    return {"detail": "Public access withdrawn"}


# ---------------------------------------------------------------------------
# Ownership transfer
#
# "Owner" is not the same shape on every resource type, and neither is what
# happens to the previous owner:
#
# - A project has exactly one ACL entry, permanently — its own group, at
#   owner — so transferring leadership means changing who holds owner
#   *standing* inside that one shared group. The previous lead is demoted to
#   admin, not removed: they're still a real member of the same ongoing
#   collaboration, just no longer in charge of it.
# - A dataset/sample/instrument's owner grant lives on the owner's own
#   *personal* ORCID group. Transferring moves the ACL row to the new
#   owner's personal group, and the old grant is removed outright — leaving
#   "Alice's personal group is admin here" behind after she no longer owns
#   it doesn't fit what a personal group means. Whatever access she keeps
#   should come from an actual standing elsewhere (e.g. project membership).
#
# See docs/permission_system_plan.md, Future Work -> Ownership transfer.
# ---------------------------------------------------------------------------

def _transfer_project_ownership(session, mfid: str, new_owner: User) -> Optional[User]:
    project = session.exec(select(Project).where(Project.unique_id == mfid)).one()
    ag = session.exec(select(AccessGroup).where(
        AccessGroup.group_name == project.unique_id,
        AccessGroup.group_type == AccessGroupType.PROJECT.value,
    )).one_or_none()
    if ag is None:
        raise HTTPException(status_code=409, detail=f"No access group for project '{project.project_id}'")

    previous_owner = None
    old_owner_links = session.exec(
        select(UserAccessGroupLink)
        .where(UserAccessGroupLink.accessgroup_id == ag.id,
              UserAccessGroupLink.role == Role.OWNER.db_value)
    ).all()
    for link in old_owner_links:
        prev_user = session.exec(select(User).where(User.id == link.user_id)).one_or_none()
        if prev_user is not None:
            previous_owner = prev_user
        link.role = Role.ADMIN.db_value
        session.add(link)
    session.flush()  # demote before promoting — one_owner_per_group must never see two at once

    new_owner_link = session.exec(
        select(UserAccessGroupLink)
        .where(UserAccessGroupLink.user_id == new_owner.id, UserAccessGroupLink.accessgroup_id == ag.id)
    ).one_or_none()
    if new_owner_link is None:
        session.add(UserAccessGroupLink(user_id=new_owner.id, accessgroup_id=ag.id,
                                        role=Role.OWNER.db_value))
    else:
        new_owner_link.role = Role.OWNER.db_value
        session.add(new_owner_link)

    project.project_lead_orcid = new_owner.unique_id
    project.project_lead_email = new_owner.email
    session.add(project)
    return previous_owner


def _transfer_resource_owner_acl(session, resource: Resource, mfid: str, new_owner: User) -> Optional[User]:
    """Unlike a project's shared group, this grant lives on the owner's own
    personal ORCID group — so the old grant is removed outright, not
    demoted. Leaving 'Alice's personal group is admin here' behind after she
    no longer owns it doesn't fit what a personal group means; any access
    she keeps should come from an actual standing (e.g. project membership),
    not a leftover grant tied to her identity group specifically.
    """
    previous_owner = None
    old_owner_acls = session.exec(
        select(ResourceACL).where(ResourceACL.mfid == mfid, ResourceACL.permission == Role.OWNER.db_value)
    ).all()
    for acl in old_owner_acls:
        old_ag = session.exec(select(AccessGroup).where(AccessGroup.id == acl.accessgroup_id)).one()
        prev_user = session.exec(select(User).where(User.unique_id == old_ag.group_name)).one_or_none()
        if prev_user is not None:
            previous_owner = prev_user
        session.delete(acl)
    session.flush()

    new_ag = session.exec(select(AccessGroup).where(
        AccessGroup.group_name == new_owner.unique_id,
        AccessGroup.group_type == AccessGroupType.IDENTITY.value,
    )).one_or_none()
    if new_ag is None:
        raise HTTPException(status_code=409,
                            detail=f"New owner '{new_owner.unique_id}' has no personal access group")
    new_owner_acl = session.exec(
        select(ResourceACL).where(ResourceACL.mfid == mfid, ResourceACL.accessgroup_id == new_ag.id)
    ).one_or_none()
    if new_owner_acl is None:
        session.add(ResourceACL(mfid=mfid, accessgroup_id=new_ag.id, read=True, write=True,
                                permission=Role.OWNER.db_value))
    else:
        new_owner_acl.permission = Role.OWNER.db_value
        new_owner_acl.write = True
        session.add(new_owner_acl)

    if resource.resource_type in ("dataset", "sample"):
        entity_model = Dataset if resource.resource_type == "dataset" else Sample
        entity = session.exec(select(entity_model).where(entity_model.unique_id == mfid)).one_or_none()
        if entity is not None:
            entity.owner_orcid = new_owner.unique_id
            entity.owner_user_id = new_owner.id
            session.add(entity)

    return previous_owner


def _current_owner(session, resource: Resource, mfid: str) -> Optional[User]:
    """Who currently holds `owner` on this resource — read-only, for the
    transfer_ownership preview. The real transfer's own demotion loop
    re-derives this independently rather than reusing it, since execution
    also has to defensively clean up more than one owner-role row if one
    ever existed (see _transfer_resource_owner_acl)."""
    if resource.resource_type == "project":
        project = session.exec(select(Project).where(Project.unique_id == mfid)).one_or_none()
        ag = (
            session.exec(select(AccessGroup).where(AccessGroup.group_name == project.unique_id)).one_or_none()
            if project else None
        )
        if ag is None:
            return None
        link = session.exec(
            select(UserAccessGroupLink)
            .where(UserAccessGroupLink.accessgroup_id == ag.id,
                  UserAccessGroupLink.role == Role.OWNER.db_value)
        ).first()
        return session.exec(select(User).where(User.id == link.user_id)).one_or_none() if link else None

    acl = session.exec(
        select(ResourceACL).where(ResourceACL.mfid == mfid, ResourceACL.permission == Role.OWNER.db_value)
    ).first()
    if acl is None:
        return None
    ag = session.exec(select(AccessGroup).where(AccessGroup.id == acl.accessgroup_id)).one_or_none()
    return session.exec(select(User).where(User.unique_id == ag.group_name)).one_or_none() if ag else None


@router.post("/resources/{mfid}/transfer_ownership", response_model=TransferOwnershipRead)
def transfer_ownership(mfid: str, body: TransferOwnershipRequest,
                       auth_res: Annotated[Principal, Depends(authorize_user)],
                       confirm: bool = False):
    """Move ownership of a resource to another user. Requires owner.

    Defaults to a preview: resolves new_owner and reports the current owner
    without changing anything. Pass ?confirm=true to actually execute. This
    is the safety net against a typo'd identifier resolving to some other
    real person — worth having because whatever happens to the previous
    owner (demoted for a project, removed outright for a dataset/sample —
    see the module comment above), they lose the owner standing that would
    let them undo it themselves. Recovery needs the (possibly wrong) new
    owner's cooperation or a platform admin.

    This is the only way to change who owns a resource. PATCH /projects
    rejects project_lead fields, and PATCH /datasets and PATCH /samples reject
    ownership fields.
    """
    with Session(engine) as session:
        resource = session.exec(select(Resource).where(Resource.mfid == mfid)).one_or_none()
        if resource is None:
            raise HTTPException(status_code=404, detail="Resource not found")

        require_permission(session, auth_res, mfid, Permission.TRANSFER)

        new_owner = resolve_user_by_identifier(session, body.new_owner)

        if not confirm:
            previous_owner = _current_owner(session, resource, mfid)
            return TransferOwnershipRead(
                resource_id=mfid,
                previous_owner=UserPublicRead.model_validate(previous_owner) if previous_owner else None,
                new_owner=UserPublicRead.model_validate(new_owner),
            )

        if resource.resource_type == "project":
            previous_owner = _transfer_project_ownership(session, mfid, new_owner)
        else:
            previous_owner = _transfer_resource_owner_acl(session, resource, mfid, new_owner)

        touch_resource_modification_time(session, mfid)
        session.commit()

        return TransferOwnershipRead(
            resource_id=mfid,
            previous_owner=UserPublicRead.model_validate(previous_owner) if previous_owner else None,
            new_owner=UserPublicRead.model_validate(new_owner),
        )


def _current_project_grants(session, mfid: str) -> List[tuple]:
    rows = session.exec(
        select(ResourceACL, AccessGroup)
        .join(AccessGroup, AccessGroup.id == ResourceACL.accessgroup_id)
        .where(ResourceACL.mfid == mfid)
    ).all()
    return [(acl, ag) for acl, ag in rows if is_project_group(ag.group_name, session)]


@router.post("/resources/{mfid}/project", response_model=ReassignProjectRead)
def reassign_resource_project(mfid: str, body: ReassignProjectRequest,
                              auth_res: Annotated[Principal, Depends(authorize_user)],
                              confirm: bool = False):
    if body.project_id is None:
        raise HTTPException(status_code=422, detail="project_id is required")

    with Session(engine) as session:
        resource = session.exec(select(Resource).where(Resource.mfid == mfid)).one_or_none()
        if resource is None:
            raise HTTPException(status_code=404, detail="Resource not found")
        if resource.resource_type not in ("dataset", "sample"):
            raise HTTPException(status_code=400,
                                detail=f"Cannot reassign project for resource_type={resource.resource_type!r}")

        require_permission(session, auth_res, mfid, Permission.MANAGE_ACL)

        new_proj = session.exec(select(Project).where(Project.project_id == body.project_id)).one_or_none()
        if new_proj is None:
            raise HTTPException(status_code=404, detail=f"Project '{body.project_id}' not found")
        require_permission(session, auth_res, new_proj.unique_id, Permission.CREATE)

        current_grants = _current_project_grants(session, mfid)
        previous_project_id = None
        if current_grants:
            _, prev_ag = current_grants[0]
            prev_proj = session.exec(select(Project).where(Project.unique_id == prev_ag.group_name)).one_or_none()
            previous_project_id = prev_proj.project_id if prev_proj else None

        if not confirm:
            return ReassignProjectRead(resource_id=mfid, previous_project_id=previous_project_id,
                                       new_project_id=body.project_id)

        new_ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == new_proj.unique_id)).one_or_none()
        if new_ag is None:
            raise HTTPException(status_code=409, detail=f"No access group for project '{body.project_id}'")

        for acl, _ in current_grants:
            session.delete(acl)
        session.flush()
        sql_create_resource_acl(new_ag, mfid, read=True, write=True, session=session, permission=Role.ADMIN)

        entity_model = Dataset if resource.resource_type == "dataset" else Sample
        entity = session.exec(select(entity_model).where(entity_model.unique_id == mfid)).one_or_none()
        if entity is not None:
            entity.project_id = body.project_id
            entity.project_mfid = new_proj.unique_id
            session.add(entity)

        touch_resource_modification_time(session, mfid)
        session.commit()

        return ReassignProjectRead(resource_id=mfid, previous_project_id=previous_project_id,
                                   new_project_id=body.project_id)

from dataclasses import dataclass
from typing import Protocol, TypeAlias

from fastapi import HTTPException
from sqlalchemy import join
from sqlmodel import Session, select

from app.models.access import EntryCapabilities
from app.models.access_groups import AccessGroupMemberRead
from app.tables import (
    AccessGroup,
    Dataset,
    DeletionRequest,
    Instrument,
    Project,
    Resource,
    Sample,
    ScientificMetadataBinary,
    User,
    UserAccessGroupLink,
)
from app.models.datasets import DatasetRead, DatasetResponse
from app.models.instrument import InstrumentResponse
from app.models.projects import ProjectWithLead
from app.models.samples import SampleResponse
from app.models.users import UserPublicRead
from app.utils.auth_utils import (
    entry_capabilities,
    fetch_acl_rows,
    require_permission,
    resource_capabilities,
)
from app.services.resource_access import load_member_grants, may_view_members, record_role, with_grant_capabilities
from app.utils.deletion_utils import has_deletion_blockers
from app.utils.instrument_utils import dataset_instrument_fields, resolve_instrument_mfids_to_references
from app.utils.link_utils import fetch_readable_links, fetch_readable_sample_datasets
from app.utils.permissions import Permission, Principal, Role, effective_role
from app.utils.project_utils import project_fields, resolve_project_mfids_to_references
from app.utils.user_utils import owner_fields, owners_of
from app.utils.sql_utils import is_public, get_active_deletion_request


@dataclass(frozen=True)
class ResourceDetailOptions:
    include_metadata: bool = False
    include_links: bool = False
    include_owner: bool = False
    include_members: bool = False
    include_datasets: bool = True


DEFAULT_RESOURCE_DETAIL_OPTIONS = ResourceDetailOptions()


def _resource(session: Session, resource_id: str, resource: Resource | None) -> Resource | None:
    if resource is not None:
        return resource
    return session.exec(
        select(Resource).where(Resource.mfid == resource_id)
    ).one_or_none()


def _metadata(session: Session, resource_id: str, include_metadata: bool) -> dict | None:
    if not include_metadata:
        return None
    row = session.exec(
        select(ScientificMetadataBinary).where(
            ScientificMetadataBinary.unique_id == resource_id
        )
    ).one_or_none()
    return row.scientific_metadata if row else None


def _deletion_request_payload(deletion_request: DeletionRequest | None) -> dict | None:
    if deletion_request is None:
        return None
    return {**deletion_request.model_dump(), "resource_mfid": deletion_request.resource_id}


def load_project_members(session: Session, project_id: str) -> list[AccessGroupMemberRead]:
    rows = session.exec(
        select(User, UserAccessGroupLink.role)
        .select_from(
            join(
                AccessGroup,
                UserAccessGroupLink,
                UserAccessGroupLink.accessgroup_id == AccessGroup.id,
            )
        )
        .join(User, User.id == UserAccessGroupLink.user_id)
        .join(Project, Project.unique_id == AccessGroup.group_name)
        .where(Project.project_id == project_id)
    ).all()
    return [
        AccessGroupMemberRead.model_validate(user, update={"role": role or "unknown"})
        for user, role in rows
    ]


def with_member_capabilities(session: Session, principal: Principal, project_mfid: str, members: list[AccessGroupMemberRead]) -> list[AccessGroupMemberRead]:
    actor = effective_role(principal, fetch_acl_rows(session, project_mfid))
    for member in members:
        current = Role.from_db(member.role) if member.role in {role.db_value for role in Role} else None
        member.capabilities = EntryCapabilities(**entry_capabilities(
            principal, actor, current, floor=Role.EDITOR, is_self=member.unique_id == principal.user_id))
    return members


def load_dataset_detail(
    session: Session,
    principal: Principal,
    resource_id: str,
    *,
    resource: Resource | None = None,
    options: ResourceDetailOptions = DEFAULT_RESOURCE_DETAIL_OPTIONS,
) -> DatasetResponse:
    if resource is None:
        result = session.exec(
            select(Dataset, Resource)
            .join(Resource, Resource.mfid == Dataset.unique_id, isouter=True)
            .where(Dataset.unique_id == resource_id)
        ).one_or_none()
        if result is None:
            raise HTTPException(status_code=404, detail="Dataset not found")
        dataset, resource = result
    else:
        dataset = session.exec(
            select(Dataset).where(Dataset.unique_id == resource_id)
        ).one_or_none()
        if dataset is None:
            raise HTTPException(status_code=404, detail="Dataset not found")
    acl_rows = fetch_acl_rows(session, resource_id)
    require_permission(
        session,
        principal,
        resource_id,
        Permission.READ,
        acl_rows=acl_rows,
    )
    deletion_request = get_active_deletion_request(session, resource_id)
    instrument_references = resolve_instrument_mfids_to_references([dataset.instrument_mfid], session)
    project_references = resolve_project_mfids_to_references([dataset.project_mfid], session)
    instrument_reference = instrument_references.get(dataset.instrument_mfid)
    project_reference = project_references.get(dataset.project_mfid)

    return DatasetResponse.model_validate({
        **dataset.model_dump(),
        **owner_fields(dataset, owners_of(session, [dataset]), options.include_owner),
        **dataset_instrument_fields(dataset, instrument_reference),
        "project_id": project_reference.project_id if project_reference else None,
        "project": project_reference,
        "public": is_public(session, dataset.unique_id),
        "creation_time": resource.creation_time if resource else None,
        "modification_time": resource.modification_time if resource else None,
        "scientific_metadata": _metadata(session, resource_id, options.include_metadata),
        "deletion_request": _deletion_request_payload(deletion_request),
        "links": fetch_readable_links(
            session,
            principal,
            "dataset",
            dataset,
        ) if options.include_links else None,
        "capabilities": resource_capabilities(
            session,
            principal,
            resource_id,
            status_permission=None,
            acl_rows=acl_rows,
            request_deletion_with=Permission.CREATE,
            deletion_requested=deletion_request is not None,
        ),
    })


def load_sample_detail(
    session: Session,
    principal: Principal,
    resource_id: str,
    *,
    resource: Resource | None = None,
    options: ResourceDetailOptions = DEFAULT_RESOURCE_DETAIL_OPTIONS,
) -> SampleResponse:
    if resource is None:
        result = session.exec(
            select(Sample, Resource)
            .join(Resource, Resource.mfid == Sample.unique_id, isouter=True)
            .where(Sample.unique_id == resource_id)
        ).one_or_none()
        if result is None:
            raise HTTPException(status_code=404, detail="Sample not found")
        sample, resource = result
    else:
        sample = session.exec(
            select(Sample).where(Sample.unique_id == resource_id)
        ).one_or_none()
        if sample is None:
            raise HTTPException(status_code=404, detail="Sample not found")
    acl_rows = fetch_acl_rows(session, resource_id)
    require_permission(
        session,
        principal,
        resource_id,
        Permission.READ,
        acl_rows=acl_rows,
    )
    deletion_request = get_active_deletion_request(session, resource_id)
    readable_datasets = (
        fetch_readable_sample_datasets(session, principal, sample.id)
        if options.include_datasets or options.include_links
        else []
    )
    instrument_references = (
        resolve_instrument_mfids_to_references(
            [dataset.instrument_mfid for dataset in readable_datasets],
            session,
        )
        if options.include_datasets
        else {}
    )
    project_references = resolve_project_mfids_to_references(
        ([dataset.project_mfid for dataset in readable_datasets] if options.include_datasets else [])
        + [sample.project_mfid],
        session,
    )
    dataset_owners = owners_of(session, readable_datasets) if options.include_datasets else {}
    return SampleResponse.model_validate({
        **sample.model_dump(),
        **owner_fields(sample, owners_of(session, [sample]), options.include_owner),
        "project_id": project_references[sample.project_mfid].project_id if sample.project_mfid in project_references else None,
        "project": project_references.get(sample.project_mfid),
        "public": is_public(session, sample.unique_id),
        "datasets": [
            DatasetRead.model_validate({
                **dataset.model_dump(),
                **owner_fields(dataset, dataset_owners),
                **dataset_instrument_fields(dataset, instrument_references.get(dataset.instrument_mfid)),
                "project_id": project_references[dataset.project_mfid].project_id if dataset.project_mfid in project_references else None,
                "project": project_references.get(dataset.project_mfid),
            })
            for dataset in readable_datasets
        ] if options.include_datasets else None,
        "creation_time": resource.creation_time if resource else None,
        "modification_time": resource.modification_time if resource else None,
        "scientific_metadata": _metadata(session, resource_id, options.include_metadata),
        "deletion_request": _deletion_request_payload(deletion_request),
        "links": fetch_readable_links(
            session,
            principal,
            "sample",
            sample,
            associated_entities=readable_datasets,
        ) if options.include_links else None,
        "capabilities": resource_capabilities(
            session,
            principal,
            resource_id,
            status_permission=None,
            acl_rows=acl_rows,
            request_deletion_with=Permission.CREATE,
            deletion_requested=deletion_request is not None,
        ),
    })


def load_instrument_detail(
    session: Session,
    principal: Principal,
    resource_id: str,
    *,
    resource: Resource | None = None,
    options: ResourceDetailOptions = DEFAULT_RESOURCE_DETAIL_OPTIONS,
) -> InstrumentResponse:
    if resource is None:
        result = session.exec(
            select(Instrument, Resource)
            .join(Resource, Resource.mfid == Instrument.unique_id, isouter=True)
            .where(Instrument.unique_id == resource_id)
        ).one_or_none()
        if result is None:
            raise HTTPException(status_code=404, detail="Instrument not found")
        instrument, resource = result
    else:
        instrument = session.exec(
            select(Instrument).where(Instrument.unique_id == resource_id)
        ).one_or_none()
        if instrument is None:
            raise HTTPException(status_code=404, detail="Instrument not found")
    acl_rows = fetch_acl_rows(session, resource_id)
    deletion_request = get_active_deletion_request(session, resource_id)
    owner = session.get(User, instrument.owner_user_id) if instrument.owner_user_id is not None else None
    return InstrumentResponse.model_validate({
        **instrument.model_dump(exclude={"owner", "owner_user_id"}),
        "owner_orcid": owner.unique_id if owner else None,
        "owner": UserPublicRead.model_validate(owner) if owner and options.include_owner else None,
        "creation_time": resource.creation_time if resource else None,
        "modification_time": resource.modification_time if resource else None,
        "scientific_metadata": _metadata(session, resource_id, options.include_metadata),
        "capabilities": resource_capabilities(
            session,
            principal,
            resource_id,
            acl_rows=acl_rows,
            can_delete=lambda: not has_deletion_blockers(session, "instrument", resource_id),
            deletion_requested=deletion_request is not None,
        ),
        "deletion_request": _deletion_request_payload(deletion_request),
        "members": with_grant_capabilities(session, principal, resource_id, load_member_grants(session, resource_id))
        if options.include_members and may_view_members(session, principal, resource_id) else None,
        "role": role.db_value if (role := record_role(principal, acl_rows)) is not None else None,
    })


def load_project_detail(
    session: Session,
    principal: Principal,
    resource_id: str,
    *,
    resource: Resource | None = None,
    options: ResourceDetailOptions = DEFAULT_RESOURCE_DETAIL_OPTIONS,
    project: Project | None = None,
) -> ProjectWithLead:
    if project is None:
        project = session.exec(
            select(Project).where(Project.unique_id == resource_id)
        ).one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    resource = _resource(session, resource_id, resource)
    is_member = principal.is_platform_admin or principal.role_in(project.unique_id) is not None
    lead = session.get(User, project.lead_user_id) if project.lead_user_id is not None else None
    deletion_request = get_active_deletion_request(session, resource_id)
    return ProjectWithLead(
        **project_fields(project, lead),
        lead=UserPublicRead.model_validate(lead) if lead else None,
        members=with_member_capabilities(session, principal, project.unique_id, load_project_members(session, project.project_id))
        if options.include_members and is_member else None,
        creation_time=resource.creation_time if resource else None,
        modification_time=resource.modification_time if resource else None,
        scientific_metadata=_metadata(
            session,
            resource_id,
            options.include_metadata and is_member,
        ),
        capabilities=resource_capabilities(
            session,
            principal,
            resource_id,
            manage_access_floor=Role.EDITOR,
            status_permission=Permission.EDIT,
            can_delete=lambda: not has_deletion_blockers(session, "project", resource_id),
            deletion_requested=deletion_request is not None,
        ),
        deletion_request=_deletion_request_payload(deletion_request),
        role=role.db_value if (role := principal.role_in(project.unique_id)) is not None else None,
    )


ResourceDetailResponse: TypeAlias = (
    DatasetResponse | SampleResponse | InstrumentResponse | ProjectWithLead
)


class ResourceDetailLoader(Protocol):
    def __call__(
        self,
        session: Session,
        principal: Principal,
        resource_id: str,
        *,
        resource: Resource | None = None,
        options: ResourceDetailOptions = DEFAULT_RESOURCE_DETAIL_OPTIONS,
    ) -> ResourceDetailResponse: ...


RESOURCE_DETAIL_LOADERS: dict[str, ResourceDetailLoader] = {
    "dataset": load_dataset_detail,
    "sample": load_sample_detail,
    "instrument": load_instrument_detail,
    "project": load_project_detail,
}


def load_resource_detail(
    session: Session,
    principal: Principal,
    resource: Resource,
    options: ResourceDetailOptions,
) -> ResourceDetailResponse:
    loader = RESOURCE_DETAIL_LOADERS.get(resource.resource_type)
    if loader is None:
        raise HTTPException(status_code=404, detail="Resource not found")
    return loader(
        session,
        principal,
        resource.mfid,
        resource=resource,
        options=options,
    )

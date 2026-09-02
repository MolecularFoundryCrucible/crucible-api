from dataclasses import dataclass
from typing import Protocol, TypeAlias

from fastapi import HTTPException
from sqlalchemy import join
from sqlmodel import Session, select

from app.models.access_groups import AccessGroupMemberRead
from app.models.core_models import (
    AccessGroup,
    Dataset,
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
    fetch_acl_rows,
    has_platform_admin_access,
    require_permission,
    resource_capabilities,
)
from app.utils.instrument_utils import resolve_instrument_mfids_to_references
from app.utils.link_utils import fetch_readable_links, fetch_readable_sample_datasets
from app.utils.permissions import Permission, Principal, Role
from app.utils.project_utils import resolve_project_mfids_to_references
from app.utils.sql_utils import get_active_deletion_request


@dataclass(frozen=True)
class ResourceDetailOptions:
    include_metadata: bool = False
    include_links: bool = False
    include_owner: bool = False
    include_members: bool = False


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


def _owner(session: Session, owner_id: str | None, include_owner: bool) -> UserPublicRead | None:
    if not include_owner or not owner_id:
        return None
    user = session.exec(
        select(User).where(User.unique_id == owner_id)
    ).one_or_none()
    return UserPublicRead.model_validate(user) if user else None


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
        "instrument_id": instrument_reference.instrument_id if instrument_reference else None,
        "project_id": project_reference.project_id if project_reference else dataset.project_id,
        "instrument": instrument_reference,
        "project": project_reference,
        "public": resource.public if resource else dataset.public,
        "creation_time": resource.creation_time if resource else None,
        "modification_time": resource.modification_time if resource else None,
        "scientific_metadata": _metadata(session, resource_id, options.include_metadata),
        "deletion_request": deletion_request.model_dump() if deletion_request else None,
        "links": fetch_readable_links(
            session,
            principal,
            "dataset",
            dataset,
        ) if options.include_links else None,
        "owner": _owner(session, dataset.owner_orcid, options.include_owner),
        "capabilities": resource_capabilities(
            session,
            principal,
            resource_id,
            status_permission=None,
            acl_rows=acl_rows,
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
    readable_datasets = fetch_readable_sample_datasets(session, principal, sample.id)
    instrument_references = resolve_instrument_mfids_to_references(
        [dataset.instrument_mfid for dataset in readable_datasets],
        session,
    )
    project_references = resolve_project_mfids_to_references(
        [dataset.project_mfid for dataset in readable_datasets] + [sample.project_mfid],
        session,
    )
    return SampleResponse.model_validate({
        **sample.model_dump(),
        "project_id": project_references[sample.project_mfid].project_id if sample.project_mfid in project_references else sample.project_id,
        "public": resource.public if resource else False,
        "datasets": [
            DatasetRead.model_validate({
                **dataset.model_dump(),
                "instrument_id": instrument_references[dataset.instrument_mfid].instrument_id if dataset.instrument_mfid in instrument_references else None,
                "project_id": project_references[dataset.project_mfid].project_id if dataset.project_mfid in project_references else dataset.project_id,
                "instrument": instrument_references.get(dataset.instrument_mfid),
                "project": project_references.get(dataset.project_mfid),
            })
            for dataset in readable_datasets
        ],
        "creation_time": resource.creation_time if resource else None,
        "modification_time": resource.modification_time if resource else None,
        "scientific_metadata": _metadata(session, resource_id, options.include_metadata),
        "deletion_request": deletion_request.model_dump() if deletion_request else None,
        "links": fetch_readable_links(
            session,
            principal,
            "sample",
            sample,
            associated_entities=readable_datasets,
        ) if options.include_links else None,
        "owner": _owner(session, sample.owner_orcid, options.include_owner),
        "capabilities": resource_capabilities(
            session,
            principal,
            resource_id,
            status_permission=None,
            acl_rows=acl_rows,
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
    legacy_group = (instrument.instrument_name or "").lower()
    return InstrumentResponse.model_validate({
        **instrument.model_dump(exclude={"owner"}),
        "owner_orcid": instrument.owner,
        "owner": _owner(session, instrument.owner, options.include_owner),
        "creation_time": resource.creation_time if resource else None,
        "modification_time": resource.modification_time if resource else None,
        "scientific_metadata": _metadata(session, resource_id, options.include_metadata),
        "capabilities": resource_capabilities(
            session,
            principal,
            resource_id,
            legacy_edit_group=legacy_group,
            legacy_status_group=legacy_group,
        ),
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
    is_member = has_platform_admin_access(principal) or principal.role_in(project.unique_id) is not None
    lead = _owner(session, project.project_lead_orcid, True)
    return ProjectWithLead(
        **project.model_dump(),
        lead=lead,
        members=load_project_members(session, project.project_id)
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
            legacy_edit_group=project.unique_id,
            manage_access_floor=Role.EDITOR,
            allow_peer_grants=False,
            status_permission=Permission.EDIT,
            legacy_status_group=project.unique_id,
        ),
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

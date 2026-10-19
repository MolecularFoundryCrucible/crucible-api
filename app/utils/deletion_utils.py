import logging

from fastapi import HTTPException
from sqlalchemy import delete, exists, func, or_
from sqlmodel import Session, select

from app.models.core_models import (
    AccessGroup, AccessGroupJoinRequest, AssociatedFile, Dataset,
    DatasetKeywordLink, DatasetLink, DatasetSampleLink, DeletionRequest,
    Instrument, IngestionRequest, PostprocessingRun, Project, Resource, ResourceACL,
    ResourceEmbedding, Sample, ScientificMetadataBinary, SampleLink, UserRecentResource,
    Thumbnail, UploadSession, UserAccessGroupLink,
)

logger = logging.getLogger(__name__)

from app.api.routes.gcs_storage import _DOWNLOAD_BUCKET
from app.utils.permissions import AccessGroupType


def _nonzero(counts: dict) -> dict:
    return {kind: count for kind, count in counts.items() if count}


def _project_deletion_summary(session: Session, project_mfid: str) -> dict:
    project = session.exec(
        select(Project).where(Project.unique_id == project_mfid)
    ).one_or_none()
    if project is None:
        raise ValueError(f"Project row missing for resource {project_mfid}")
    group = session.exec(
        select(AccessGroup).where(
            AccessGroup.group_name == project.unique_id,
            AccessGroup.group_type == AccessGroupType.PROJECT.value,
        )
    ).one_or_none()
    summary = {
        "blocking": _nonzero({
            "datasets": session.exec(
                select(func.count(Dataset.id)).where(Dataset.project_mfid == project.unique_id)
            ).one(),
            "samples": session.exec(
                select(func.count(Sample.id)).where(
                    Sample.project_mfid == project.unique_id
                )
            ).one(),
        }),
        "memberships_removed": 0,
        "join_requests_removed": 0,
        "removed_grants": [],
    }
    if group is not None:
        summary["memberships_removed"] = session.exec(
            select(func.count(UserAccessGroupLink.user_id)).where(
                UserAccessGroupLink.accessgroup_id == group.id
            )
        ).one()
        summary["join_requests_removed"] = session.exec(
            select(func.count(AccessGroupJoinRequest.id)).where(
                AccessGroupJoinRequest.accessgroup_id == group.id
            )
        ).one()
        summary["removed_grants"] = [
            grant for grant in _deleted_group_grants(session, group)
            if grant["resource_mfid"] != project_mfid
        ]
    return summary


def _instrument_deletion_summary(session: Session, instrument_mfid: str) -> dict:
    instrument = session.exec(
        select(Instrument).where(Instrument.unique_id == instrument_mfid)
    ).one_or_none()
    if instrument is None:
        raise ValueError(f"Instrument row missing for resource {instrument_mfid}")
    group = session.exec(
        select(AccessGroup).where(
            AccessGroup.group_name == instrument.unique_id,
            AccessGroup.group_type == AccessGroupType.INSTRUMENT.value,
        )
    ).one_or_none()
    summary = {
        "blocking": _nonzero({
            "datasets": session.exec(
                select(func.count(Dataset.id)).where(Dataset.instrument_mfid == instrument.unique_id)
            ).one(),
        }),
        "service_accounts_unbound": 0,
        "removed_grants": [],
    }
    if group is not None:
        summary["service_accounts_unbound"] = session.exec(
            select(func.count(UserAccessGroupLink.user_id)).where(
                UserAccessGroupLink.accessgroup_id == group.id
            )
        ).one()
        summary["removed_grants"] = [
            grant for grant in _deleted_group_grants(session, group)
            if grant["resource_mfid"] != instrument_mfid
        ]
    return summary


def deletion_summary(session: Session, resource_type: str, mfid: str) -> dict:
    """Blocking assignment counts plus what deleting a project or instrument would remove."""
    if resource_type == "project":
        return _project_deletion_summary(session, mfid)
    if resource_type == "instrument":
        return _instrument_deletion_summary(session, mfid)
    raise ValueError(f"Deletion summary is not defined for resource type '{resource_type}'")


def has_deletion_blockers(session: Session, resource_type: str, mfid: str) -> bool:
    """Whether any dataset or sample is still assigned, with the same rules as deletion_summary's counts."""
    if resource_type == "project":
        project = session.exec(select(Project).where(Project.unique_id == mfid)).one()
        return session.exec(select(
            exists().where(Dataset.project_mfid == project.unique_id)
            | exists().where(Sample.project_mfid == project.unique_id)
        )).one()
    if resource_type == "instrument":
        instrument = session.exec(select(Instrument).where(Instrument.unique_id == mfid)).one()
        return session.exec(select(
            exists().where(Dataset.instrument_mfid == instrument.unique_id)
        )).one()
    return False


def deletion_preview_read(
    resource_id: str,
    resource_type: str,
    deleted: bool,
    summary: dict,
):
    from app.models.deletion import DeletionGrantRead, DeletableResourceType, DeletionPreviewRead

    return DeletionPreviewRead(
        resource_id=resource_id,
        resource_mfid=resource_id,
        resource_type=DeletableResourceType(resource_type),
        deleted=deleted,
        blocking=summary.get("blocking", {}),
        memberships_removed=summary.get("memberships_removed", 0),
        service_accounts_unbound=summary.get("service_accounts_unbound", 0),
        join_requests_removed=summary.get("join_requests_removed", 0),
        removed_grants=[
            DeletionGrantRead(**grant) for grant in summary.get("removed_grants", [])
        ],
    )


def assert_not_pending_deletion(session: Session, mfid: str) -> None:
    active = session.exec(
        select(DeletionRequest)
        .where(DeletionRequest.resource_id == mfid)
        .where(DeletionRequest.status.in_(["pending", "approved"]))
    ).first()
    if active is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"'{mfid}' has an active deletion request "
                f"(status: {active.status})."
            ),
        )


def _deleted_group_grants(session: Session, group: AccessGroup) -> list[dict]:
    rows = session.exec(
        select(Resource.mfid, Resource.resource_type, ResourceACL.permission)
        .join(Resource, Resource.mfid == ResourceACL.mfid)
        .where(ResourceACL.accessgroup_id == group.id)
    ).all()
    return [
        {"resource_mfid": mfid, "resource_type": resource_type, "permission": permission}
        for mfid, resource_type, permission in rows
    ]


def _delete_gcs_blobs_for_dataset(dataset_unique_id: str, associated_files: list) -> None:
    """Delete GCS blobs for a dataset's associated files. Logs but never raises."""
    try:
        from app.api.routes.gcs_storage import _get_gcs_storage_client
        client = _get_gcs_storage_client()
        bucket = client.bucket(_DOWNLOAD_BUCKET)
    except Exception:
        logger.exception(
            "GCS client init failed for dataset %s — DB deletion will still proceed",
            dataset_unique_id,
        )
        return

    for af in associated_files:
        if not af.storage_path:
            continue
        try:
            blob_name = af.storage_path.partition("/")[2]
            blob = bucket.blob(blob_name)
            if blob.exists():
                blob.delete()
                logger.info("Deleted GCS blob %s", blob_name)
        except Exception:
            logger.exception(
                "Failed to delete GCS blob for file %s (dataset %s) — continuing",
                af.storage_path, dataset_unique_id,
            )


def _hard_delete_dataset(dataset_unique_id: str, session: Session, resource: Resource) -> None:
    dataset = session.exec(select(Dataset).where(Dataset.unique_id == dataset_unique_id).with_for_update()).one_or_none()
    if dataset is None:
        raise RuntimeError(f"Dataset row missing for resource {dataset_unique_id}")

    session.execute(delete(IngestionRequest).where(
        or_(
            IngestionRequest.dataset_mfid == dataset_unique_id,
            IngestionRequest.file_mfid.in_(
                select(AssociatedFile.mfid).where(AssociatedFile.dataset_id == dataset.id)
            ),
        )
    ))
    session.execute(delete(PostprocessingRun).where(PostprocessingRun.dataset_mfid == dataset_unique_id))
    session.execute(delete(UploadSession).where(UploadSession.dataset_mfid == dataset_unique_id))
    session.execute(delete(AssociatedFile).where(AssociatedFile.dataset_id == dataset.id))
    session.execute(delete(DatasetKeywordLink).where(DatasetKeywordLink.dataset_id == dataset.id))
    session.execute(delete(Thumbnail).where(Thumbnail.dataset_id == dataset.id))
    session.execute(delete(DatasetSampleLink).where(DatasetSampleLink.dataset_id == dataset.id))
    session.execute(delete(DatasetLink).where(
        or_(DatasetLink.parent_dataset_id == dataset.id, DatasetLink.child_dataset_id == dataset.id)
    ))
    session.execute(delete(ScientificMetadataBinary).where(
        ScientificMetadataBinary.unique_id == dataset_unique_id
    ))
    session.execute(delete(ResourceACL).where(ResourceACL.mfid == dataset_unique_id))
    session.execute(delete(DeletionRequest).where(DeletionRequest.resource_id == dataset_unique_id))

    session.execute(delete(Dataset).where(Dataset.id == dataset.id))
    session.execute(delete(Resource).where(Resource.mfid == dataset_unique_id))


def _hard_delete_sample(sample_unique_id: str, session: Session, resource: Resource) -> None:
    sample = session.exec(select(Sample).where(Sample.unique_id == sample_unique_id)).one_or_none()
    if sample is None:
        raise RuntimeError(f"Sample row missing for resource {sample_unique_id}")

    session.execute(delete(DatasetSampleLink).where(DatasetSampleLink.sample_id == sample.id))
    session.execute(delete(SampleLink).where(
        or_(SampleLink.sample_id == sample.id, SampleLink.parent_sample_id == sample.id)
    ))
    session.execute(delete(ScientificMetadataBinary).where(
        ScientificMetadataBinary.unique_id == sample_unique_id
    ))
    session.execute(delete(ResourceACL).where(ResourceACL.mfid == sample_unique_id))
    session.execute(delete(DeletionRequest).where(DeletionRequest.resource_id == sample_unique_id))

    session.execute(delete(Sample).where(Sample.id == sample.id))
    session.execute(delete(Resource).where(Resource.mfid == sample_unique_id))


def _hard_delete_project(project_unique_id: str, session: Session, resource: Resource) -> dict:
    """Caller owns the transaction. Returns the deletion summary."""
    project = session.exec(
        select(Project).where(Project.unique_id == project_unique_id)
    ).one_or_none()
    if project is None:
        raise RuntimeError(f"Project row missing for resource {project_unique_id}")

    group = session.exec(
        select(AccessGroup).where(
            AccessGroup.group_name == project.unique_id,
            AccessGroup.group_type == AccessGroupType.PROJECT.value,
        )
    ).one_or_none()

    summary = _project_deletion_summary(session, project_unique_id)
    if group is not None:
        session.execute(delete(ResourceACL).where(
            ResourceACL.accessgroup_id == group.id
        ))
        session.execute(delete(UserAccessGroupLink).where(
            UserAccessGroupLink.accessgroup_id == group.id
        ))
        session.execute(delete(AccessGroupJoinRequest).where(
            AccessGroupJoinRequest.accessgroup_id == group.id
        ))
        session.execute(delete(AccessGroup).where(AccessGroup.id == group.id))

    session.execute(delete(ScientificMetadataBinary).where(
        ScientificMetadataBinary.unique_id == project_unique_id
    ))
    session.execute(delete(ResourceACL).where(ResourceACL.mfid == project_unique_id))
    session.execute(delete(DeletionRequest).where(
        DeletionRequest.resource_id == project_unique_id
    ))
    session.execute(delete(Project).where(Project.id == project.id))
    session.execute(delete(Resource).where(Resource.mfid == project_unique_id))
    return summary


def _hard_delete_instrument(instrument_unique_id: str, session: Session, resource: Resource) -> dict:
    """Caller owns the transaction. Returns the deletion summary."""
    instrument = session.exec(
        select(Instrument).where(Instrument.unique_id == instrument_unique_id)
    ).one_or_none()
    if instrument is None:
        raise RuntimeError(f"Instrument row missing for resource {instrument_unique_id}")

    group = session.exec(
        select(AccessGroup).where(
            AccessGroup.group_name == instrument.unique_id,
            AccessGroup.group_type == AccessGroupType.INSTRUMENT.value,
        )
    ).one_or_none()

    summary = _instrument_deletion_summary(session, instrument_unique_id)
    if group is not None:
        session.execute(delete(ResourceACL).where(
            ResourceACL.accessgroup_id == group.id
        ))
        session.execute(delete(UserAccessGroupLink).where(
            UserAccessGroupLink.accessgroup_id == group.id
        ))
        session.execute(delete(AccessGroup).where(AccessGroup.id == group.id))

    session.execute(delete(ScientificMetadataBinary).where(
        ScientificMetadataBinary.unique_id == instrument_unique_id
    ))
    session.execute(delete(ResourceACL).where(ResourceACL.mfid == instrument_unique_id))
    session.execute(delete(DeletionRequest).where(
        DeletionRequest.resource_id == instrument_unique_id
    ))
    session.execute(delete(Instrument).where(Instrument.id == instrument.id))
    session.execute(delete(Resource).where(Resource.mfid == instrument_unique_id))
    return summary


def hard_delete_resource(resource_id: str, session: Session, resource: Resource | None = None):
    """Delete a supported resource's database rows. Caller owns the transaction."""
    resource = resource or session.exec(select(Resource).where(Resource.mfid == resource_id)).one_or_none()
    if resource is None:
        raise RuntimeError(f"Resource {resource_id} not found")
    session.execute(
        delete(ResourceEmbedding).where(ResourceEmbedding.resource_mfid == resource_id)
    )
    session.execute(
        delete(UserRecentResource).where(UserRecentResource.resource_mfid == resource_id)
    )
    if resource.resource_type == "dataset":
        _hard_delete_dataset(resource_id, session, resource)
    elif resource.resource_type == "sample":
        _hard_delete_sample(resource_id, session, resource)
    elif resource.resource_type == "project":
        return _hard_delete_project(resource_id, session, resource)
    elif resource.resource_type == "instrument":
        return _hard_delete_instrument(resource_id, session, resource)
    else:
        raise ValueError(f"Unsupported resource type: {resource.resource_type}")

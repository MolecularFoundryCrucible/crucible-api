import logging
from sqlalchemy import delete, or_, update, func
from sqlmodel import Session, select

from app.models.core_models import (
    AssociatedFile, Dataset, DatasetKeywordLink, DatasetLink,
    DatasetSampleLink, DeletionRequest, IngestionRequest, Resource,
    ResourceACL, Sample, ScientificMetadataBinary,
    SampleLink, Thumbnail, UploadSession,
)

logger = logging.getLogger(__name__)

from app.api.routes.gcs_storage import _DOWNLOAD_BUCKET


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
    session.execute(update(UploadSession).where(
        UploadSession.dataset_mfid == dataset_unique_id,
        UploadSession.protocol.is_not(None),
        UploadSession.cleanup_requested_at.is_(None),
    ).values(cleanup_requested_at=func.now(), next_attempt_at=func.now(), cleaned_at=None))
    session.execute(delete(UploadSession).where(
        UploadSession.dataset_mfid == dataset_unique_id,
        UploadSession.protocol.is_(None),
    ))
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


def hard_delete_resource(resource_id: str, session: Session, resource: Resource | None = None) -> None:
    """Delete a supported resource's database rows. Caller owns the transaction."""
    resource = resource or session.exec(select(Resource).where(Resource.mfid == resource_id)).one_or_none()
    if resource is None:
        raise RuntimeError(f"Resource {resource_id} not found")
    if resource.resource_type == "dataset":
        _hard_delete_dataset(resource_id, session, resource)
    elif resource.resource_type == "sample":
        _hard_delete_sample(resource_id, session, resource)
    else:
        raise ValueError(f"Unsupported resource type: {resource.resource_type}")

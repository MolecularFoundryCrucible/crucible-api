import logging
from sqlalchemy import or_, text
from sqlmodel import Session, select

from app.models.core_models import (
    AssociatedFile, Dataset, DatasetKeywordLink, DatasetLink,
    DatasetSampleLink, DeletionRequest, IngestionRequest, Resource,
    ResourceACL, Sample, ScientificMetadata, ScientificMetadataBinary,
    SampleLink, Thumbnail,
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
    dataset = session.exec(select(Dataset).where(Dataset.unique_id == dataset_unique_id)).one_or_none()
    if dataset is None:
        return

    associated_files = session.exec(
        select(AssociatedFile).where(AssociatedFile.dataset_id == dataset.id)
    ).all()

    for ing in session.exec(
        select(IngestionRequest).where(IngestionRequest.dataset_mfid == dataset_unique_id)
    ).all():
        session.delete(ing)

    for af in associated_files:
        session.delete(af)

    for link in session.exec(
        select(DatasetKeywordLink).where(DatasetKeywordLink.dataset_id == dataset.id)
    ).all():
        session.delete(link)

    for thumb in session.exec(
        select(Thumbnail).where(Thumbnail.dataset_id == dataset.id)
    ).all():
        session.delete(thumb)

    for link in session.exec(
        select(DatasetSampleLink).where(DatasetSampleLink.dataset_id == dataset.id)
    ).all():
        session.delete(link)

    for link in session.exec(
        select(DatasetLink).where(
            or_(DatasetLink.parent_dataset_id == dataset.id, DatasetLink.child_dataset_id == dataset.id)
        )
    ).all():
        session.delete(link)

    smb = session.exec(
        select(ScientificMetadataBinary).where(ScientificMetadataBinary.unique_id == dataset_unique_id)
    ).one_or_none()
    if smb:
        session.delete(smb)

    sm = session.exec(
        select(ScientificMetadata).where(ScientificMetadata.dataset_unique_id == dataset_unique_id)
    ).one_or_none()
    if sm:
        session.delete(sm)

    for acl in session.exec(
        select(ResourceACL).where(ResourceACL.mfid == dataset_unique_id)
    ).all():
        session.delete(acl)

    # Legacy table not in ORM — delete by dataset integer id if rows exist
    session.execute(text("DELETE FROM datasetacl WHERE dataset_id = :did").bindparams(did=dataset.id))

    for dr in session.exec(
        select(DeletionRequest).where(DeletionRequest.resource_id == dataset_unique_id)
    ).all():
        session.delete(dr)

    session.delete(resource)
    session.delete(dataset)


def _hard_delete_sample(sample_unique_id: str, session: Session, resource: Resource) -> None:
    sample = session.exec(select(Sample).where(Sample.unique_id == sample_unique_id)).one_or_none()
    if sample is None:
        return

    for link in session.exec(
        select(DatasetSampleLink).where(DatasetSampleLink.sample_id == sample.id)
    ).all():
        session.delete(link)

    for link in session.exec(
        select(SampleLink).where(
            or_(SampleLink.sample_id == sample.id, SampleLink.parent_sample_id == sample.id)
        )
    ).all():
        session.delete(link)

    smb = session.exec(
        select(ScientificMetadataBinary).where(ScientificMetadataBinary.unique_id == sample_unique_id)
    ).one_or_none()
    if smb:
        session.delete(smb)

    for acl in session.exec(
        select(ResourceACL).where(ResourceACL.mfid == sample_unique_id)
    ).all():
        session.delete(acl)

    for dr in session.exec(
        select(DeletionRequest).where(DeletionRequest.resource_id == sample_unique_id)
    ).all():
        session.delete(dr)

    session.delete(resource)
    session.delete(sample)


def hard_delete_resource(resource_id: str, session: Session) -> None:
    """Determine resource type and dispatch to the appropriate hard delete. Caller must commit."""
    resource = session.exec(select(Resource).where(Resource.mfid == resource_id)).one_or_none()
    if resource is None:
        return
    if resource.resource_type == "dataset":
        _hard_delete_dataset(resource_id, session, resource)
    elif resource.resource_type == "sample":
        _hard_delete_sample(resource_id, session, resource)
    else:
        logger.warning(
            "hard_delete_resource called on unsupported resource_type %r for %s — skipping",
            resource.resource_type, resource_id,
        )

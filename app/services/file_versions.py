from dataclasses import dataclass
from datetime import timedelta, timezone

from sqlmodel import Session, select

from app.config import get_settings
from app.tables import AssociatedFile, Dataset, FileVersion, IngestionRequest
from app.services.storage_downloads import owned_object
from app.storage import StoreUnavailable, store_config
from app.utils.sql_utils import file_named, get_active_deletion_request, touch_resource_modification_time


class RestoreRefused(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def waiting():
    return FileVersion.deleted_at.is_(None), FileVersion.restored_at.is_(None)


def utc(value):
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def keep_version(session: Session, file: AssociatedFile, dataset_mfid: str, when) -> FileVersion | None:
    """Record the file's current content as a version, in the caller's transaction, before the file is
    repointed. Only objects the API owns and has a version for are kept: anything else (another
    environment's bucket, a legacy object without a generation) is never deleted by the cleanup."""
    return _keep(session, file, dataset_mfid, when, "replaced")


def trash_file(session: Session, file: AssociatedFile, dataset_mfid: str, when) -> FileVersion | None:
    """Record the content of a file about to be deleted, with its filename and ingestion requests so a
    restore can bring it back, in the caller's transaction, under the same rules as keep_version."""
    return _keep(session, file, dataset_mfid, when, "deleted")


def _keep(session, file, dataset_mfid, when, reason):
    if not file.storage_path or file.storage_generation is None:
        return None
    try:
        store_id = store_config(file.store_id).id
        if not owned_object(store_config(file.store_id), file.storage_path, file.storage_generation, dataset_mfid):
            return None
    except StoreUnavailable:
        # Kept so it can be restored or cleaned once the store is configured again; cleanup checks ownership then.
        store_id = file.store_id
    deadline = when + timedelta(days=get_settings().storage.file_version_retention_days)
    existing = session.exec(select(FileVersion).where(FileVersion.storage_path == file.storage_path, *waiting())).first()
    if existing is not None:
        # Another file shared this object and was kept earlier: this one gets its full grace period too.
        existing.delete_after = max(utc(existing.delete_after), deadline)
        session.add(existing)
        return existing
    version = FileVersion(
        file_mfid=file.mfid, dataset_mfid=dataset_mfid, storage_path=file.storage_path, storage_generation=file.storage_generation,
        size=file.size, sha256_hash=file.sha256_hash, storage_crc32c=file.storage_crc32c, reason=reason,
        store_id=store_id, created_at=when, delete_after=deadline,
    )
    if reason == "deleted":
        version.filename = file.filename
        requests = session.exec(select(IngestionRequest).where(IngestionRequest.file_mfid == file.mfid).order_by(IngestionRequest.id)).all()
        version.ingestion_requests = [request.model_dump() for request in requests]
    session.add(version)
    return version


@dataclass(frozen=True)
class Restored:
    file_mfid: str
    storage_path: str
    version_id: int
    replaced_version_id: int | None


def restore_latest(engine, stores, file_mfid: str, when) -> Restored:
    """Make the file's newest kept version current again; the content it replaces becomes a version
    in turn, so a restore can itself be undone."""
    from app.services.multipart import replace_in_progress

    with Session(engine) as session:
        file = session.exec(select(AssociatedFile).where(AssociatedFile.mfid == file_mfid).with_for_update()).one_or_none()
        if file is None:
            raise RestoreRefused("file_not_found")
        dataset_mfid = session.exec(select(Dataset.unique_id).where(Dataset.id == file.dataset_id)).one()
        if get_active_deletion_request(session, dataset_mfid) is not None:
            raise RestoreRefused("dataset_pending_deletion")
        if replace_in_progress(session, file_mfid) is not None:
            raise RestoreRefused("replace_in_progress")
        version = session.exec(
            select(FileVersion).where(FileVersion.file_mfid == file_mfid, *waiting()).order_by(FileVersion.created_at.desc(), FileVersion.id.desc()).with_for_update()
        ).first()
        if version is None:
            raise RestoreRefused("no_kept_version")
        _check_restorable(stores, version, when)
        replaced = keep_version(session, file, dataset_mfid, when)
        file.storage_path = version.storage_path
        file.storage_generation = version.storage_generation
        file.size = version.size
        file.sha256_hash = version.sha256_hash
        file.storage_crc32c = version.storage_crc32c
        file.sha256_verified_at = None
        file.store_id = version.store_id
        version.restored_at = when
        session.add_all([file, version])
        touch_resource_modification_time(session, dataset_mfid)
        session.flush()
        result = Restored(file_mfid=file.mfid, storage_path=file.storage_path, version_id=version.id, replaced_version_id=replaced.id if replaced else None)
        session.commit()
        return result


def _check_restorable(stores, version, when) -> None:
    """Refuse a version past its grace period, which cleanup may delete at any moment (an older one is
    never used instead), or one whose stored object is gone or changed."""
    if utc(version.delete_after) <= when:
        raise RestoreRefused("version_expired")
    bucket, _, key = version.storage_path.partition("/")
    stored = stores(version.store_id).head(bucket, key)
    if stored is None:
        raise RestoreRefused("version_missing_in_storage")
    if stored.size != version.size:
        raise RestoreRefused("version_size_mismatch")


def deleted_versions(dataset_mfid: str):
    """Deleted files of the dataset whose content is still kept, newest first."""
    return select(FileVersion).where(FileVersion.dataset_mfid == dataset_mfid, FileVersion.reason == "deleted", *waiting()).order_by(FileVersion.created_at.desc(), FileVersion.id.desc())


def _deleted_version(session, dataset_mfid, file_mfid):
    version = session.exec(deleted_versions(dataset_mfid).where(FileVersion.file_mfid == file_mfid).with_for_update()).first()
    if version is None:
        raise RestoreRefused("not_in_trash")
    return version


def restore_deleted(engine, stores, dataset_mfid: str, file_mfid: str, when) -> AssociatedFile:
    """Bring a deleted file back with its MFID, content and ingestion requests while its content is kept."""
    from app.services.multipart import filename_reserved

    with Session(engine) as session:
        version = _deleted_version(session, dataset_mfid, file_mfid)
        _check_restorable(stores, version, when)
        dataset = session.exec(select(Dataset).where(Dataset.unique_id == dataset_mfid).with_for_update()).one_or_none()
        if dataset is None:
            raise RestoreRefused("not_in_trash")
        if get_active_deletion_request(session, dataset_mfid) is not None:
            raise RestoreRefused("dataset_pending_deletion")
        if file_named(session, dataset.id, version.filename) is not None:
            raise RestoreRefused("filename_exists")
        if filename_reserved(session, dataset_mfid, version.filename):
            raise RestoreRefused("filename_reserved")
        file = AssociatedFile(
            mfid=version.file_mfid, dataset_id=dataset.id, filename=version.filename, storage_path=version.storage_path,
            storage_generation=version.storage_generation, size=version.size, sha256_hash=version.sha256_hash, storage_crc32c=version.storage_crc32c,
            store_id=version.store_id,
        )
        session.add(file)
        session.flush()
        for values in version.ingestion_requests or []:
            # PostgreSQL never hands an id out twice, but a database that reuses ids may have; then a new one keeps the request.
            reused = session.get(IngestionRequest, values["id"]) is not None
            session.add(IngestionRequest(**{**values, "id": None} if reused else values))
        version.restored_at = when
        session.add(version)
        touch_resource_modification_time(session, dataset_mfid)
        session.commit()
        session.refresh(file)
        session.expunge(file)
        return file


def purge_deleted(session: Session, dataset_mfid: str, file_mfid: str, when) -> None:
    """End a deleted file's grace period now, so the next cleanup removes its content; in the caller's transaction."""
    version = _deleted_version(session, dataset_mfid, file_mfid)
    version.delete_after = when
    session.add(version)

from dataclasses import dataclass
from datetime import timedelta, timezone

from sqlmodel import Session, select

from app.config import get_settings
from app.models.core_models import AssociatedFile, Dataset, FileVersion, UploadSession
from app.services.storage_downloads import owned_object
from app.utils.sql_utils import get_active_deletion_request, touch_resource_modification_time


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
    if not file.storage_path or file.storage_generation is None or not owned_object(file.storage_path, file.storage_generation, dataset_mfid):
        return None
    deadline = when + timedelta(days=get_settings().storage.file_version_retention_days)
    existing = session.exec(select(FileVersion).where(FileVersion.storage_path == file.storage_path, *waiting())).first()
    if existing is not None:
        # Another file shared this object and was replaced earlier: this replace gets its full grace period too.
        existing.delete_after = max(utc(existing.delete_after), deadline)
        session.add(existing)
        return existing
    version = FileVersion(
        file_mfid=file.mfid, dataset_mfid=dataset_mfid, storage_path=file.storage_path, storage_generation=file.storage_generation,
        size=file.size, sha256_hash=file.sha256_hash, storage_crc32c=file.storage_crc32c, reason="replaced",
        created_at=when, delete_after=deadline,
    )
    session.add(version)
    return version


@dataclass(frozen=True)
class Restored:
    file_mfid: str
    storage_path: str
    version_id: int
    replaced_version_id: int | None


def restore_latest(engine, store, file_mfid: str, when) -> Restored:
    """Make the file's newest kept version current again; the content it replaces becomes a version
    in turn, so a restore can itself be undone."""
    from app.services.multipart import live

    with Session(engine) as session:
        file = session.exec(select(AssociatedFile).where(AssociatedFile.mfid == file_mfid).with_for_update()).one_or_none()
        if file is None:
            raise RestoreRefused("file_not_found")
        dataset_mfid = session.exec(select(Dataset.unique_id).where(Dataset.id == file.dataset_id)).one()
        if get_active_deletion_request(session, dataset_mfid) is not None:
            raise RestoreRefused("dataset_pending_deletion")
        if session.exec(select(UploadSession.id).where(UploadSession.replace_file_mfid == file_mfid, *live())).first() is not None:
            raise RestoreRefused("replace_in_progress")
        version = session.exec(
            select(FileVersion).where(FileVersion.file_mfid == file_mfid, *waiting()).order_by(FileVersion.created_at.desc(), FileVersion.id.desc()).with_for_update()
        ).first()
        if version is None:
            raise RestoreRefused("no_kept_version")
        # A version past its grace period may be deleted at any moment; never fall back to an older one.
        if utc(version.delete_after) <= when:
            raise RestoreRefused("version_expired")
        bucket, _, key = version.storage_path.partition("/")
        stored = store.head(bucket, key)
        if stored is None:
            raise RestoreRefused("version_missing_in_storage")
        if stored.size != version.size:
            raise RestoreRefused("version_size_mismatch")
        replaced = keep_version(session, file, dataset_mfid, when)
        file.storage_path = version.storage_path
        file.storage_generation = version.storage_generation
        file.size = version.size
        file.sha256_hash = version.sha256_hash
        file.storage_crc32c = version.storage_crc32c
        file.sha256_verified_at = None
        version.restored_at = when
        session.add_all([file, version])
        touch_resource_modification_time(session, dataset_mfid)
        session.flush()
        result = Restored(file_mfid=file.mfid, storage_path=file.storage_path, version_id=version.id, replaced_version_id=replaced.id if replaced else None)
        session.commit()
        return result

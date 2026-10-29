import logging
from dataclasses import dataclass, field
from datetime import timedelta

from sqlmodel import Session, select

from app.models.core_models import AssociatedFile, FileVersion, UploadSession
from app.services import multipart
from app.services.file_versions import waiting
from app.services.storage_downloads import owned_object
from app.storage import StorageFailure

logger = logging.getLogger(__name__)
ABANDONED = ("aborted", "expired", "failed")
# A completion can still be running when an upload passes its expiry; give it this long to finish.
COMPLETION_MARGIN = timedelta(hours=1)


@dataclass
class CleanupReport:
    aborted_uploads: int = 0
    deleted_versions: int = 0
    kept_in_use: int = 0
    failures: list[str] = field(default_factory=list)


def run_cleanup(engine, store, now, dry_run: bool = False, batch: int = 500) -> CleanupReport:
    """Abort uploads that expired or were left uncleaned, and delete file versions whose grace period
    has passed and that no file uses. Each item is claimed under a row lock and committed on its own,
    so concurrent runs skip each other's work; items are visited in id order once per run, so one
    that fails or is kept never holds back the others. `batch` caps the uploads and versions handled."""
    report = CleanupReport()
    clean_uploads(engine, store, now, dry_run, batch, report)
    delete_versions(engine, store, now, dry_run, batch, report)
    return report


def due_uploads(now):
    return (
        UploadSession.protocol == multipart.PROTOCOL,
        UploadSession.cleaned_at.is_(None),
        (UploadSession.state.notin_(multipart.TERMINAL) & (UploadSession.expires_at <= now - COMPLETION_MARGIN)) | UploadSession.state.in_(ABANDONED),
    )


def clean_uploads(engine, store, now, dry_run, batch, report):
    if dry_run:
        with Session(engine) as session:
            report.aborted_uploads = len(session.exec(select(UploadSession.id).where(*due_uploads(now)).limit(batch)).all())
        return
    last = handled = 0
    while handled < batch:
        with Session(engine) as session:
            upload = session.exec(
                select(UploadSession).where(*due_uploads(now), UploadSession.id > last).order_by(UploadSession.id).limit(1).with_for_update(skip_locked=True)
            ).first()
            if upload is None:
                return
            last = upload.id
            if upload.state not in multipart.TERMINAL:
                upload.state = "expired"
                multipart.save(session, upload)
            session.commit()
            session.refresh(upload)
            session.expunge(upload)
        handled += 1
        try:
            multipart.cleanup(engine, store, upload)
            report.aborted_uploads += 1
        except StorageFailure as error:
            report.failures.append(f"upload {upload.upload_id}: {error or 'storage_failure'}")


def delete_versions(engine, store, now, dry_run, batch, report):
    due = (*waiting(), FileVersion.delete_after <= now)
    last = 0
    while report.deleted_versions < batch:
        with Session(engine) as session:
            query = select(FileVersion).where(*due, FileVersion.id > last).order_by(FileVersion.id).limit(1)
            if not dry_run:
                query = query.with_for_update(skip_locked=True)
            version = session.exec(query).first()
            if version is None:
                return
            last = version.id
            if session.exec(select(AssociatedFile.id).where(AssociatedFile.storage_path == version.storage_path)).first() is not None:
                report.kept_in_use += 1
                continue
            if not owned_object(version.storage_path, version.storage_generation, version.dataset_mfid):
                report.failures.append(f"version {version.id}: not_owned")
                if not dry_run:
                    version.failure_code = "not_owned"
                    session.add(version)
                    session.commit()
                continue
            if dry_run:
                report.deleted_versions += 1
                continue
            bucket, _, key = version.storage_path.partition("/")
            try:
                store.delete(bucket, key, version.storage_generation)
            except StorageFailure as error:
                version.attempts += 1
                version.failure_code = str(error) or "storage_failure"
                session.add(version)
                session.commit()
                report.failures.append(f"version {version.id}: {version.failure_code}")
                continue
            version.deleted_at = now
            version.failure_code = None
            session.add(version)
            session.commit()
            report.deleted_versions += 1
            logger.info("file_version_deleted version=%s file=%s path=%s", version.id, version.file_mfid, version.storage_path)

import hashlib
import json
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import func, or_, text
from sqlmodel import Session, select

from app.models.core_models import AssociatedFile, Dataset, UploadSession, User
from app.models.datasets import AssociatedFileRead
from app.models.multipart import MultipartStatus
from app.utils.auth_utils import load_principal, require_permission
from app.utils.permissions import Permission, PlatformRole

PROTOCOL = "gcs_xml_v1"
PART_SIZE = 64 * 1024 ** 2
TERMINAL = ("completed", "aborted", "expired", "failed")
LEASE_SECONDS = 600
logger = logging.getLogger(__name__)


def now():
    return datetime.now(timezone.utc)


def utc(value):
    value = datetime.fromisoformat(value) if isinstance(value, str) else value
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def save(session, upload):
    upload.updated_at = now()
    upload.version = (upload.version or 0) + 1
    session.add(upload)


def authorize(session, principal, dsid):
    current = load_principal(session, principal.user_id)
    if current.is_platform_admin and not principal.is_platform_admin:
        current = replace(current, platform_role=PlatformRole.NONE)
    require_permission(session, current, dsid, Permission.CREATE)


def owned(session, dsid, upload_id, principal, *, lock=False, check_access=True):
    query = select(UploadSession).where(UploadSession.upload_id == upload_id)
    if lock:
        query = query.with_for_update()
    upload = session.exec(query).one_or_none()
    if upload is None:
        raise HTTPException(404, "Upload session not found")
    if upload.owner_user_id != principal.user_id:
        raise HTTPException(403, "Not the owner of this upload session")
    if upload.dataset_mfid != dsid:
        raise HTTPException(404, "Upload session not found")
    if upload.protocol != PROTOCOL:
        raise HTTPException(409, "Use the legacy completion route for this upload session")
    if check_access:
        if session.exec(select(Dataset.id).where(Dataset.unique_id == dsid)).first() is None:
            raise HTTPException(404, "Dataset not found")
        authorize(session, principal, dsid)
    return upload


def active(upload):
    if utc(upload.expires_at) <= now():
        raise HTTPException(410, "Upload session has expired")
    if upload.cleanup_requested_at is not None or upload.state in ("aborting", "aborted", "expired", "failed"):
        raise HTTPException(409, "Upload is being cleaned up or is terminal")


def part_size(upload, number):
    return min(upload.part_size, upload.size - (number - 1) * upload.part_size)


def file_read(session, upload):
    file = session.exec(select(AssociatedFile).where(AssociatedFile.mfid == upload.result_file_mfid)).one_or_none()
    if file is None:
        raise HTTPException(410, "The completed file has been deleted")
    return AssociatedFileRead.model_validate(file, update={"dataset_mfid": upload.dataset_mfid})


def worker_principal(session, upload):
    principal = load_principal(session, upload.owner_user_id)
    if principal.is_platform_admin and not (upload.part_manifest or {}).get("allow_global_admin", False):
        principal = replace(principal, platform_role=PlatformRole.NONE)
    return principal


def status(session, upload):
    return MultipartStatus(upload_id=upload.upload_id, state=upload.state, expires_at=utc(upload.expires_at), part_size=upload.part_size, part_count=upload.part_count, failure_code=upload.failure_code, existing_file=file_read(session, upload) if upload.state == "completed" else None)


def initiate(engine, principal, dsid, body, key, config):
    if body.size > config.max_upload_bytes:
        raise HTTPException(413, "File exceeds the configured upload limit")
    fingerprint = hashlib.sha256(json.dumps(body.model_dump(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    with Session(engine) as session:
        user = session.exec(select(User).where(User.unique_id == principal.user_id).with_for_update()).one_or_none()
        if user is None:
            raise HTTPException(401, "Unknown principal")
        dataset = session.exec(select(Dataset).where(Dataset.unique_id == dsid).with_for_update()).one_or_none()
        if dataset is None:
            raise HTTPException(404, "Dataset not found")
        authorize(session, principal, dsid)
        if key:
            previous = session.exec(select(UploadSession).where(UploadSession.owner_user_id == principal.user_id, UploadSession.dataset_mfid == dsid, UploadSession.protocol == PROTOCOL, UploadSession.idempotency_key == key)).one_or_none()
            if previous:
                if previous.request_fingerprint != fingerprint:
                    raise HTTPException(409, "Idempotency key was already used for a different request")
                return status(session, previous)
        if body.sha256_hash:
            existing = session.exec(select(AssociatedFile).where(AssociatedFile.dataset_id == dataset.id, AssociatedFile.sha256_hash == body.sha256_hash, AssociatedFile.size == body.size, AssociatedFile.sha256_verified_at.is_not(None))).first()
            if existing:
                require_permission(session, load_principal(session, principal.user_id), dsid, Permission.READ)
                return MultipartStatus(state="completed", existing_file=AssociatedFileRead.model_validate(existing, update={"dataset_mfid": dsid}))
        if session.exec(select(AssociatedFile.id).where(AssociatedFile.dataset_id == dataset.id, AssociatedFile.filename == body.filename)).first() is not None:
            raise HTTPException(409, "Filename already exists")
        if session.exec(select(UploadSession.id).where(UploadSession.dataset_mfid == dsid, UploadSession.protocol == PROTOCOL, UploadSession.associated_file_name == body.filename, UploadSession.state.notin_(TERMINAL))).first() is not None:
            raise HTTPException(409, "Filename is reserved by another upload")
        for column, value, count_limit, byte_limit in ((UploadSession.owner_user_id, principal.user_id, config.owner_sessions, config.owner_bytes), (UploadSession.dataset_mfid, dsid, config.dataset_sessions, config.dataset_bytes)):
            count, size = session.exec(select(func.count(UploadSession.id), func.coalesce(func.sum(UploadSession.size), 0)).where(column == value, UploadSession.protocol == PROTOCOL, or_(UploadSession.state.notin_(TERMINAL), (UploadSession.cleanup_requested_at.is_not(None) & UploadSession.cleaned_at.is_(None))))).one()
            if count >= count_limit or size + body.size > byte_limit:
                raise HTTPException(429, "Upload reservation quota exceeded", headers={"Retry-After": "60"})
        identifier = uuid4().hex
        upload = UploadSession(upload_id=identifier, dataset_mfid=dsid, owner_user_id=principal.user_id, gcs_path=f"uploads/{dsid}/{identifier}/content", associated_file_name=body.filename, expires_at=(now() + timedelta(days=7)).isoformat(), size=body.size, protocol=PROTOCOL, bucket=config.bucket, state="initiating", part_size=PART_SIZE, part_count=(body.size + PART_SIZE - 1) // PART_SIZE, idempotency_key=key, request_fingerprint=fingerprint, claimed_sha256=body.sha256_hash, part_manifest={"authorized": {}, "allow_global_admin": principal.is_platform_admin}, created_at=now(), updated_at=now(), version=0, next_attempt_at=now(), attempts=0)
        session.add(upload)
        session.commit()
        return status(session, upload)


class LeaseLost(Exception):
    pass


class CleanupRequested(Exception):
    pass


class InvalidUpload(Exception):
    pass


class MultipartWorker:
    def __init__(self, engine, storage, config):
        self.engine = engine
        self.storage = storage
        self.config = config
        self.identity = uuid4().hex

    def claim(self):
        with Session(self.engine) as session:
            if self.engine.dialect.name == "postgresql":
                session.execute(text("SELECT pg_advisory_xact_lock(736190281)"))
            busy = session.exec(select(func.count(UploadSession.id)).where(UploadSession.protocol == PROTOCOL, UploadSession.lease_expires_at > now())).one()
            if busy >= self.config.worker_concurrency:
                return None
            query = select(UploadSession).where(UploadSession.protocol == PROTOCOL, or_(UploadSession.lease_expires_at.is_(None), UploadSession.lease_expires_at < now()), or_(UploadSession.next_attempt_at.is_(None), UploadSession.next_attempt_at <= now())).order_by(UploadSession.next_attempt_at, UploadSession.id).with_for_update(skip_locked=True).limit(1)
            upload = session.exec(query).first()
            if upload is None:
                return None
            upload.lease_owner = self.identity
            upload.lease_expires_at = now() + timedelta(seconds=LEASE_SECONDS)
            save(session, upload)
            session.commit()
            session.refresh(upload)
            session.expunge(upload)
            return upload

    def locked(self, session, identifier, allow_cleanup=False):
        upload = session.exec(select(UploadSession).where(UploadSession.upload_id == identifier).with_for_update()).one()
        if upload.lease_owner != self.identity or utc(upload.lease_expires_at) <= now():
            raise LeaseLost()
        if upload.cleanup_requested_at is not None and not allow_cleanup:
            raise CleanupRequested()
        return upload

    def heartbeat(self, identifier, allow_cleanup=False):
        with Session(self.engine) as session:
            upload = self.locked(session, identifier, allow_cleanup)
            upload.lease_expires_at = now() + timedelta(seconds=LEASE_SECONDS)
            session.add(upload)
            session.commit()

    def update(self, identifier, **values):
        with Session(self.engine) as session:
            upload = self.locked(session, identifier)
            for key, value in values.items():
                setattr(upload, key, value)
            save(session, upload)
            session.commit()
            session.refresh(upload)
            session.expunge(upload)
            return upload

    def release(self, identifier, delay=60, expected_schedule=None):
        with Session(self.engine) as session:
            upload = self.locked(session, identifier, True)
            upload.lease_owner = None
            upload.lease_expires_at = None
            if expected_schedule is not None and utc(upload.next_attempt_at) != utc(expected_schedule):
                delay = 0
            upload.next_attempt_at = now() + timedelta(seconds=delay)
            save(session, upload)
            session.commit()

    def fail(self, identifier, code):
        with Session(self.engine) as session:
            upload = self.locked(session, identifier, True)
            upload.state = "failed"
            upload.failure_code = code
            upload.cleanup_requested_at = upload.cleanup_requested_at or now()
            save(session, upload)
            session.commit()

    def check_access(self, upload):
        with Session(self.engine) as session:
            if session.exec(select(Dataset.id).where(Dataset.unique_id == upload.dataset_mfid)).first() is None:
                raise InvalidUpload("dataset_deleted")
            try:
                require_permission(session, worker_principal(session, upload), upload.dataset_mfid, Permission.CREATE)
            except HTTPException:
                raise InvalidUpload("access_revoked") from None

    def cleanup(self, upload):
        self.heartbeat(upload.upload_id, True)
        for provider_id in set(self.storage.uploads(upload)) | ({upload.provider_upload_id} if upload.provider_upload_id else set()):
            self.heartbeat(upload.upload_id, True)
            self.storage.abort(upload, provider_id)
        metadata = self.storage.metadata(upload)
        if metadata:
            with Session(self.engine) as session:
                current = self.locked(session, upload.upload_id, True)
                registered = session.exec(select(AssociatedFile.id).where(AssociatedFile.storage_path == f"{upload.bucket}/{upload.gcs_path}", AssociatedFile.storage_generation.is_not(None))).first()
                if registered is not None:
                    raise InvalidUpload("cleanup_object_still_registered")
                if current.storage_generation and current.storage_generation != metadata["generation"]:
                    raise InvalidUpload("cleanup_generation_changed")
            self.storage.delete(upload, metadata["generation"])
        with Session(self.engine) as session:
            current = self.locked(session, upload.upload_id, True)
            current.cleaned_at = now()
            if current.state not in ("failed", "expired", "completed"):
                current.state = "aborted"
            save(session, current)
            session.commit()

    def begin(self, upload):
        self.heartbeat(upload.upload_id)
        identifiers = sorted(self.storage.uploads(upload))
        provider_id = identifiers[0] if identifiers else self.storage.initiate(upload)
        upload = self.update(upload.upload_id, provider_upload_id=provider_id, state="uploading", attempts=0, failure_code=None)
        for duplicate in identifiers[1:]:
            self.heartbeat(upload.upload_id)
            self.storage.abort(upload, duplicate)

    def finalize(self, upload):
        metadata = self.storage.metadata(upload)
        if metadata is None:
            manifest = dict(upload.part_manifest or {})
            parts = manifest.get("frozen")
            if parts is None:
                parts, marker = [], 0
                while True:
                    self.heartbeat(upload.upload_id)
                    page, marker = self.storage.list_parts(upload, marker)
                    parts.extend(page)
                    if len(parts) > upload.part_count:
                        raise InvalidUpload("unexpected_parts")
                    if marker is None:
                        break
                if [part["part_number"] for part in parts] != list(range(1, upload.part_count + 1)):
                    raise InvalidUpload("missing_parts")
                authorized = manifest.get("authorized", {})
                if any(part["size"] != part_size(upload, part["part_number"]) or str(part["part_number"]) not in authorized for part in parts):
                    raise InvalidUpload("part_size_or_authorization_mismatch")
                manifest["frozen"] = parts
                upload = self.update(upload.upload_id, part_manifest=manifest)
            self.heartbeat(upload.upload_id)
            self.storage.complete(upload, parts)
            metadata = self.storage.metadata(upload)
        if metadata is None:
            raise InvalidUpload("completed_object_missing")
        if metadata["size"] != upload.size or metadata["crc32c"] != upload.claimed_crc32c:
            raise InvalidUpload("object_integrity_mismatch")
        with Session(self.engine) as session:
            dataset = session.exec(select(Dataset).where(Dataset.unique_id == upload.dataset_mfid).with_for_update()).one_or_none()
            if dataset is None:
                raise InvalidUpload("dataset_deleted")
            current = self.locked(session, upload.upload_id)
            if utc(current.expires_at) <= now():
                raise InvalidUpload("session_expired")
            try:
                require_permission(session, worker_principal(session, current), current.dataset_mfid, Permission.CREATE)
            except HTTPException:
                raise InvalidUpload("access_revoked") from None
            if current.state != "finalizing":
                raise LeaseLost()
            if session.exec(select(AssociatedFile.id).where(AssociatedFile.dataset_id == dataset.id, AssociatedFile.filename == current.associated_file_name)).first() is not None:
                raise InvalidUpload("filename_conflict")
            file = AssociatedFile(dataset_id=dataset.id, filename=current.associated_file_name, storage_path=f"{current.bucket}/{current.gcs_path}", size=current.size, sha256_hash=current.claimed_sha256, storage_generation=metadata["generation"], storage_crc32c=current.claimed_crc32c)
            session.add(file)
            current.result_file_mfid = file.mfid
            current.storage_generation = metadata["generation"]
            current.state = "completed"
            current.failure_code = None
            save(session, current)
            session.commit()

    def run_once(self):
        upload = self.claim()
        if upload is None:
            return False
        delay = 0
        logger.info("multipart_work upload_id=%s state=%s", upload.upload_id, upload.state)
        try:
            if upload.cleanup_requested_at is not None or upload.state in ("aborting", "aborted", "expired", "failed"):
                self.cleanup(upload)
                delay = 3600
            elif upload.state == "completed":
                for provider_id in self.storage.uploads(upload):
                    self.heartbeat(upload.upload_id)
                    self.storage.abort(upload, provider_id)
                delay = 3600
            elif utc(upload.expires_at) <= now():
                with Session(self.engine) as session:
                    current = self.locked(session, upload.upload_id)
                    current.state = "expired"
                    current.cleanup_requested_at = now()
                    save(session, current)
                    session.commit()
            else:
                self.check_access(upload)
                if upload.state == "initiating":
                    self.begin(upload)
                    delay = 60
                elif upload.state == "uploading":
                    identifiers = self.storage.uploads(upload)
                    if upload.provider_upload_id not in identifiers:
                        raise InvalidUpload("provider_upload_missing")
                    for duplicate in identifiers:
                        if duplicate != upload.provider_upload_id:
                            self.heartbeat(upload.upload_id)
                            self.storage.abort(upload, duplicate)
                    delay = 60
                elif upload.state == "finalizing":
                    self.finalize(upload)
                    delay = 3600
        except (LeaseLost, CleanupRequested):
            delay = 0
        except InvalidUpload as error:
            logger.warning("multipart_failed upload_id=%s code=%s", upload.upload_id, str(error))
            if upload.cleanup_requested_at is not None:
                with Session(self.engine) as session:
                    current = self.locked(session, upload.upload_id, True)
                    current.failure_code = str(error)
                    save(session, current)
                    session.commit()
                delay = 3600
            else:
                self.fail(upload.upload_id, str(error))
        except Exception:
            logger.warning("multipart_retry upload_id=%s", upload.upload_id)
            with Session(self.engine) as session:
                current = self.locked(session, upload.upload_id, True)
                current.attempts = (current.attempts or 0) + 1
                current.failure_code = "provider_retry_pending"
                save(session, current)
                session.commit()
                delay = min(3600, 5 * 2 ** min(current.attempts, 10))
        finally:
            try:
                self.release(upload.upload_id, delay, upload.next_attempt_at)
            except LeaseLost:
                pass
        return True

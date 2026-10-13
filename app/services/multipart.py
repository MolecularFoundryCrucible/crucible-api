import hashlib
import json
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import func, or_
from sqlmodel import Session, select

from app.models.core_models import AssociatedFile, Dataset, UploadSession, User
from app.models.datasets import AssociatedFileRead
from app.models.multipart import MultipartStatus
from app.services.multipart_storage import StorageFailure, UploadMissing
from app.services.storage_downloads import multipart_prefix
from app.utils.auth_utils import load_principal, require_permission
from app.utils.permissions import Permission, PlatformRole

PROTOCOL = "gcs_xml_v1"
PART_SIZE = 64 * 1024 ** 2
TERMINAL = ("completed", "aborted", "expired", "failed")
SESSION_LIFETIME = timedelta(days=7)
INITIATION_WINDOW = timedelta(minutes=10)
logger = logging.getLogger(__name__)


class InvalidUpload(Exception):
    pass


def now():
    return datetime.now(timezone.utc)


def utc(value):
    value = datetime.fromisoformat(value) if isinstance(value, str) else value
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def save(session, upload):
    upload.updated_at = now()
    upload.version = (upload.version or 0) + 1
    session.add(upload)


def demoted(session, principal):
    current = load_principal(session, principal.user_id)
    if current.is_platform_admin and not principal.is_platform_admin:
        current = replace(current, platform_role=PlatformRole.NONE)
    return current


def authorize(session, principal, dsid):
    require_permission(session, demoted(session, principal), dsid, Permission.CREATE)


def live():
    return UploadSession.protocol == PROTOCOL, UploadSession.state.notin_(TERMINAL), UploadSession.expires_at > now().isoformat()


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


def expired(upload):
    return upload.state not in TERMINAL and utc(upload.expires_at) <= now()


def active(upload):
    if expired(upload):
        raise HTTPException(410, "Upload session has expired")
    if upload.state in ("aborting", "aborted", "expired", "failed"):
        raise HTTPException(409, "Upload is aborted or failed")


def part_size(upload, number):
    return min(upload.part_size, upload.size - (number - 1) * upload.part_size)


def trusted_hash():
    return or_(AssociatedFile.storage_generation.is_(None), AssociatedFile.sha256_verified_at.is_not(None))


def filename_reserved(session, dsid, filename):
    return session.exec(select(UploadSession.id).where(UploadSession.dataset_mfid == dsid, UploadSession.associated_file_name == filename, *live())).first() is not None


def file_read(session, upload):
    file = session.exec(select(AssociatedFile).where(AssociatedFile.mfid == upload.result_file_mfid)).one_or_none()
    if file is None:
        raise HTTPException(410, "The completed file has been deleted")
    return AssociatedFileRead.model_validate(file, update={"dataset_mfid": upload.dataset_mfid})


def status(session, upload):
    return MultipartStatus(upload_id=upload.upload_id, state="expired" if expired(upload) else upload.state, expires_at=utc(upload.expires_at), part_size=upload.part_size, part_count=upload.part_count, failure_code=upload.failure_code, existing_file=file_read(session, upload) if upload.state == "completed" else None)


def unavailable(message):
    return HTTPException(503, message, headers={"Retry-After": "5"})


def reserve(engine, principal, dsid, body, key, config):
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
                if previous.state == "initiating" and not expired(previous):
                    session.expunge(previous)
                    return previous
                return status(session, previous)
        if body.sha256_hash:
            existing = session.exec(select(AssociatedFile).where(AssociatedFile.dataset_id == dataset.id, AssociatedFile.sha256_hash == body.sha256_hash, AssociatedFile.size == body.size, AssociatedFile.sha256_verified_at.is_not(None))).first()
            if existing:
                require_permission(session, demoted(session, principal), dsid, Permission.READ)
                return MultipartStatus(state="completed", existing_file=AssociatedFileRead.model_validate(existing, update={"dataset_mfid": dsid}))
        if session.exec(select(AssociatedFile.id).where(AssociatedFile.dataset_id == dataset.id, AssociatedFile.filename == body.filename)).first() is not None:
            raise HTTPException(409, "Filename already exists")
        if filename_reserved(session, dsid, body.filename):
            raise HTTPException(409, "Filename is reserved by another upload")
        for column, value, count_limit, byte_limit in ((UploadSession.owner_user_id, principal.user_id, config.owner_sessions, config.owner_bytes), (UploadSession.dataset_mfid, dsid, config.dataset_sessions, config.dataset_bytes)):
            count, size = session.exec(select(func.count(UploadSession.id), func.coalesce(func.sum(UploadSession.size), 0)).where(column == value, *live())).one()
            if count >= count_limit or size + body.size > byte_limit:
                raise HTTPException(429, "Upload reservation quota exceeded", headers={"Retry-After": "60"})
        identifier = uuid4().hex
        upload = UploadSession(upload_id=identifier, dataset_mfid=dsid, owner_user_id=principal.user_id, gcs_path=f"{multipart_prefix(dsid)}{identifier}", associated_file_name=body.filename, expires_at=(now() + INITIATION_WINDOW).isoformat(), size=body.size, protocol=PROTOCOL, bucket=config.bucket, state="initiating", part_size=PART_SIZE, part_count=(body.size + PART_SIZE - 1) // PART_SIZE, idempotency_key=key, request_fingerprint=fingerprint, claimed_sha256=body.sha256_hash, part_manifest={"authorized": {}}, created_at=now(), updated_at=now(), version=0)
        session.add(upload)
        session.commit()
        session.refresh(upload)
        session.expunge(upload)
        return upload


def initiate(engine, storage, principal, dsid, body, key, config):
    upload = reserve(engine, principal, dsid, body, key, config)
    if isinstance(upload, MultipartStatus):
        return upload
    try:
        identifiers = sorted(storage().uploads(upload))
        provider_id = identifiers[0] if identifiers else storage().initiate(upload)
    except StorageFailure:
        raise unavailable("Storage is temporarily unavailable; retry with the same Idempotency-Key") from None
    with Session(engine) as session:
        current = session.exec(select(UploadSession).where(UploadSession.upload_id == upload.upload_id).with_for_update()).one()
        if current.state == "initiating":
            current.provider_upload_id = provider_id
            current.state = "uploading"
            current.expires_at = (now() + SESSION_LIFETIME).isoformat()
            save(session, current)
            session.commit()
        return status(session, current)


def assemble(storage, upload):
    metadata = storage.metadata(upload)
    if metadata is None:
        parts, marker = [], 0
        try:
            while True:
                page, marker = storage.list_parts(upload, marker)
                parts.extend(page)
                if len(parts) > upload.part_count:
                    raise InvalidUpload("unexpected_parts")
                if marker is None:
                    break
        except UploadMissing:
            parts = None
        if parts is not None:
            if [part["part_number"] for part in parts] != list(range(1, upload.part_count + 1)):
                raise InvalidUpload("missing_parts")
            authorized = (upload.part_manifest or {}).get("authorized", {})
            if any(part["size"] != part_size(upload, part["part_number"]) or str(part["part_number"]) not in authorized for part in parts):
                raise InvalidUpload("part_size_or_authorization_mismatch")
            try:
                storage.complete(upload, parts)
            except UploadMissing:
                pass
        metadata = storage.metadata(upload)
        if metadata is None:
            raise InvalidUpload("provider_upload_missing")
    if metadata["size"] != upload.size or metadata["crc32c"] != upload.claimed_crc32c:
        raise InvalidUpload("object_integrity_mismatch")
    return metadata


def cleanup(engine, storage, upload):
    for provider_id in set(storage.uploads(upload)) | ({upload.provider_upload_id} if upload.provider_upload_id else set()):
        storage.abort(upload, provider_id)
    metadata = storage.metadata(upload)
    if metadata:
        with Session(engine) as session:
            registered = session.exec(select(AssociatedFile.id).where(AssociatedFile.storage_path == f"{upload.bucket}/{upload.gcs_path}", AssociatedFile.storage_generation.is_not(None))).first()
        if registered is None:
            storage.delete(upload, metadata["generation"])
    with Session(engine) as session:
        current = session.exec(select(UploadSession).where(UploadSession.upload_id == upload.upload_id).with_for_update()).one()
        current.cleaned_at = now()
        save(session, current)
        session.commit()


def fail(engine, storage, upload, code):
    logger.warning("multipart_failed upload_id=%s code=%s", upload.upload_id, code)
    with Session(engine) as session:
        current = session.exec(select(UploadSession).where(UploadSession.upload_id == upload.upload_id).with_for_update()).one()
        if current.state == "completed":
            return current.state
        if current.state not in TERMINAL:
            current.state = "failed"
            current.failure_code = code
            save(session, current)
            session.commit()
        state = current.state
    try:
        cleanup(engine, storage, upload)
    except StorageFailure:
        logger.warning("multipart_cleanup_deferred upload_id=%s", upload.upload_id)
    return state


def complete(engine, storage, principal, dsid, body):
    with Session(engine) as session:
        upload = owned(session, dsid, body.upload_id, principal, lock=True)
        if upload.claimed_sha256 and upload.claimed_sha256 != body.sha256_hash:
            raise HTTPException(409, "SHA-256 claim cannot be changed")
        if upload.claimed_crc32c and upload.claimed_crc32c != body.crc32c:
            raise HTTPException(409, "CRC32C claim cannot be changed")
        if upload.state == "completed":
            return file_read(session, upload)
        active(upload)
        if upload.state == "uploading":
            if len((upload.part_manifest or {}).get("authorized", {})) != upload.part_count:
                raise HTTPException(409, "All parts must be authorized before completion")
            upload.claimed_sha256 = body.sha256_hash
            upload.claimed_crc32c = body.crc32c
            upload.state = "finalizing"
            save(session, upload)
            session.commit()
        elif upload.state != "finalizing":
            raise HTTPException(409, "Upload is not ready for completion")
        session.refresh(upload)
        session.expunge(upload)
    try:
        metadata = assemble(storage(), upload)
    except InvalidUpload as error:
        if fail(engine, storage(), upload, str(error)) == "aborted":
            raise HTTPException(409, "Upload was aborted during completion") from None
        raise HTTPException(422, f"Upload failed verification: {error}") from None
    except StorageFailure:
        raise unavailable("Storage is temporarily unavailable; retry completion") from None
    with Session(engine) as session:
        dataset = session.exec(select(Dataset).where(Dataset.unique_id == dsid).with_for_update()).one_or_none()
        current = session.exec(select(UploadSession).where(UploadSession.upload_id == upload.upload_id).with_for_update()).one()
        if current.state == "completed":
            return file_read(session, current)
        if current.state != "finalizing":
            raise HTTPException(409, "Upload was aborted during completion")
        if dataset is None:
            code = "dataset_deleted"
        else:
            authorize(session, principal, dsid)
            conflict = session.exec(select(AssociatedFile.id).where(AssociatedFile.dataset_id == dataset.id, AssociatedFile.filename == current.associated_file_name)).first()
            code = "filename_conflict" if conflict is not None else None
        if code is None:
            file = AssociatedFile(dataset_id=dataset.id, filename=current.associated_file_name, storage_path=f"{current.bucket}/{current.gcs_path}", size=current.size, sha256_hash=current.claimed_sha256, storage_generation=metadata["generation"], storage_crc32c=current.claimed_crc32c)
            session.add(file)
            current.result_file_mfid = file.mfid
            current.storage_generation = metadata["generation"]
            current.state = "completed"
            current.failure_code = None
            save(session, current)
            session.commit()
            result = file_read(session, current)
    if code is not None:
        fail(engine, storage(), upload, code)
        raise HTTPException(409, f"Upload could not be published: {code}")
    try:
        for provider_id in storage().uploads(upload):
            storage().abort(upload, provider_id)
    except StorageFailure:
        logger.warning("multipart_leftover_abort_deferred upload_id=%s", upload.upload_id)
    return result


def abort(engine, storage, principal, dsid, upload_id):
    with Session(engine) as session:
        upload = owned(session, dsid, upload_id, principal, lock=True, check_access=False)
        if upload.state == "completed":
            raise HTTPException(409, "Delete the completed file through the file API")
        if upload.state in TERMINAL and upload.cleaned_at is not None:
            return
        if upload.state not in TERMINAL:
            upload.state = "aborted"
            save(session, upload)
            session.commit()
        session.refresh(upload)
        session.expunge(upload)
    try:
        cleanup(engine, storage(), upload)
    except StorageFailure:
        raise unavailable("Storage cleanup is temporarily unavailable; retry the abort") from None

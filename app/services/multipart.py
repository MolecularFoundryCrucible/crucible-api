import hashlib
import json
import logging
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import func, or_
from sqlmodel import Session, select

from app.general_utils import if_match_satisfied
from app.models.core_models import AssociatedFile, Dataset, UploadSession, User
from app.models.datasets import AssociatedFileRead, file_etag
from app.models.multipart import MultipartStatus
from app.services.multipart_storage import StorageFailure, UploadMissing
from app.services.storage_downloads import StorageLocationError, StorageNotConfigured, multipart_prefix, resolve_storage_object, upload_bucket
from app.utils.auth_utils import load_principal, require_permission
from app.utils.permissions import Permission, PlatformRole
from app.utils.sql_utils import assert_not_pending_deletion, get_active_deletion_request, touch_resource_modification_time

PROTOCOL = "gcs_xml_v1"
MIB = 1024 ** 2
MIN_PART_SIZE = 8 * MIB
PART_SIZE = 64 * MIB
PARALLEL_PARTS = 8
TERMINAL = ("completed", "aborted", "expired", "failed")
SESSION_LIFETIME = timedelta(days=7)
INITIATION_WINDOW = timedelta(minutes=10)
logger = logging.getLogger(__name__)


class InvalidUpload(Exception):
    pass


def now():
    return datetime.now(timezone.utc)


def utc(value):
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
    return UploadSession.protocol == PROTOCOL, UploadSession.state.notin_(TERMINAL), UploadSession.expires_at > now()


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


def idempotent(session, principal, dsid, key, fingerprint):
    previous = session.exec(select(UploadSession).where(UploadSession.owner_user_id == principal.user_id, UploadSession.dataset_mfid == dsid, UploadSession.protocol == PROTOCOL, UploadSession.idempotency_key == key)).one_or_none()
    if previous is None:
        return None
    if previous.request_fingerprint != fingerprint:
        raise HTTPException(409, "Idempotency key was already used for a different request")
    if previous.state == "initiating" and not expired(previous):
        session.expunge(previous)
        return previous
    return status(session, previous)


def choose_part_size(size):
    return min(PART_SIZE, max(MIN_PART_SIZE, -(-size // (PARALLEL_PARTS * MIB)) * MIB))


def new_session(session, principal, dsid, size, key, fingerprint, claimed_sha256, config, filename, replace_file_mfid=None, replace_base_etag=None):
    for column, value, count_limit, byte_limit in ((UploadSession.owner_user_id, principal.user_id, config.owner_sessions, config.owner_bytes), (UploadSession.dataset_mfid, dsid, config.dataset_sessions, config.dataset_bytes)):
        count, reserved = session.exec(select(func.count(UploadSession.id), func.coalesce(func.sum(UploadSession.size), 0)).where(column == value, *live())).one()
        if count >= count_limit or reserved + size > byte_limit:
            raise HTTPException(429, "Upload reservation quota exceeded", headers={"Retry-After": "60"})
    identifier = uuid4().hex
    part_size = choose_part_size(size)
    upload = UploadSession(upload_id=identifier, dataset_mfid=dsid, owner_user_id=principal.user_id, gcs_path=f"{multipart_prefix(dsid)}{identifier}", associated_file_name=filename, expires_at=now() + INITIATION_WINDOW, size=size, protocol=PROTOCOL, bucket=config.bucket, state="initiating", part_size=part_size, part_count=(size + part_size - 1) // part_size, idempotency_key=key, request_fingerprint=fingerprint, claimed_sha256=claimed_sha256, part_manifest={"authorized": {}}, created_at=now(), updated_at=now(), version=0, replace_file_mfid=replace_file_mfid, replace_base_etag=replace_base_etag)
    session.add(upload)
    session.commit()
    session.refresh(upload)
    session.expunge(upload)
    return upload


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
        assert_not_pending_deletion(session, dsid)
        if key:
            previous = idempotent(session, principal, dsid, key, fingerprint)
            if previous is not None:
                return previous
        existing_name = session.exec(select(AssociatedFile).where(AssociatedFile.dataset_id == dataset.id, AssociatedFile.filename == body.filename)).first()
        if existing_name is not None and body.sha256_hash and existing_name.sha256_hash == body.sha256_hash and existing_name.size == body.size and existing_name.sha256_verified_at is not None:
            require_permission(session, demoted(session, principal), dsid, Permission.READ)
            return MultipartStatus(state="completed", existing_file=AssociatedFileRead.model_validate(existing_name, update={"dataset_mfid": dsid}))
        if existing_name is not None:
            raise HTTPException(409, {"code": "filename_exists", "file_mfid": existing_name.mfid, "etag": file_etag(existing_name.storage_path, existing_name.storage_generation, existing_name.sha256_hash, existing_name.size), "message": "Filename already exists"})
        if filename_reserved(session, dsid, body.filename):
            raise HTTPException(409, "Filename is reserved by another upload")
        return new_session(session, principal, dsid, body.size, key, fingerprint, body.sha256_hash, config, body.filename)


def initiate(engine, storage, principal, dsid, body, key, config):
    upload = reserve(engine, principal, dsid, body, key, config)
    if isinstance(upload, MultipartStatus):
        return upload
    return start(engine, storage, upload)


def start(engine, storage, upload):
    try:
        identifiers = sorted(storage().uploads(upload))
        provider_id = identifiers[0] if identifiers else storage().initiate(upload)
    except StorageFailure:
        raise unavailable("Storage is temporarily unavailable; retry with the same Idempotency-Key") from None
    with Session(engine) as session:
        current = session.exec(select(UploadSession).where(UploadSession.upload_id == upload.upload_id).with_for_update()).one_or_none()
        if current is None:
            try:
                storage().abort(upload, provider_id)
            except StorageFailure:
                logger.warning("multipart_leftover_abort_deferred upload_id=%s", upload.upload_id)
            raise HTTPException(409, "Upload could not be started: dataset_deleted")
        if current.state == "initiating":
            current.provider_upload_id = provider_id
            current.state = "uploading"
            current.expires_at = now() + SESSION_LIFETIME
            save(session, current)
            session.commit()
        return status(session, current)


def reserve_replace(engine, principal, file_mfid, body, key, if_match, config, backfill=None):
    if body.size > config.max_upload_bytes:
        raise HTTPException(413, "File exceeds the configured upload limit")
    fingerprint = hashlib.sha256(json.dumps(body.model_dump() | {"replace_file_mfid": file_mfid}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    with Session(engine) as session:
        user = session.exec(select(User).where(User.unique_id == principal.user_id).with_for_update()).one_or_none()
        if user is None:
            raise HTTPException(401, "Unknown principal")
        probe_dataset_id = session.exec(select(AssociatedFile.dataset_id).where(AssociatedFile.mfid == file_mfid)).one_or_none()
        if probe_dataset_id is None:
            raise HTTPException(404, "File not found")
        dataset = session.exec(select(Dataset).where(Dataset.id == probe_dataset_id).with_for_update()).one_or_none()
        if dataset is None:
            raise HTTPException(404, "File not found")
        dsid = dataset.unique_id
        file = session.exec(select(AssociatedFile).where(AssociatedFile.mfid == file_mfid).with_for_update().execution_options(populate_existing=True)).one_or_none()
        if file is None:
            raise HTTPException(404, "File not found")
        current = demoted(session, principal)
        require_permission(session, current, dsid, Permission.READ)
        require_permission(session, current, dsid, Permission.EDIT)
        assert_not_pending_deletion(session, dsid)
        if file.storage_backend != "gcs":
            raise HTTPException(400, "Only GCS files can be replaced; edit catalog-only records with PATCH /files/{file_id}")
        if key:
            previous = idempotent(session, principal, dsid, key, fingerprint)
            if previous is not None:
                return previous
        seen_etag = file_etag(file.storage_path, file.storage_generation, file.sha256_hash, file.size)
        if not if_match_satisfied(if_match, seen_etag):
            raise HTTPException(412, {"code": "etag_mismatch", "etag": seen_etag, "message": "File has changed since the given ETag"})
        backfilled = False
        if not file.storage_path and backfill is not None:
            try:
                backfill(file, dsid, session)
                backfilled = True
            except HTTPException:
                pass
        base_etag = file_etag(file.storage_path, file.storage_generation, file.sha256_hash, file.size)
        running = session.exec(select(UploadSession).where(UploadSession.replace_file_mfid == file_mfid, *live())).first()
        if running is not None:
            if backfilled:
                session.commit()
            raise HTTPException(409, {"code": "replace_in_progress", "upload_id": running.upload_id, "message": "Another replace of this file is in progress"})
        trusted = file.storage_path and (file.storage_generation is None or file.sha256_verified_at is not None)
        if body.sha256_hash and body.sha256_hash == file.sha256_hash and body.size == file.size and trusted:
            if backfilled:
                session.commit()
            return MultipartStatus(state="completed", existing_file=AssociatedFileRead.model_validate(file, update={"dataset_mfid": dsid}))
        return new_session(session, principal, dsid, body.size, key, fingerprint, body.sha256_hash, config, file.filename, replace_file_mfid=file_mfid, replace_base_etag=base_etag)


def initiate_replace(engine, storage, principal, file_mfid, body, key, if_match, config, backfill=None):
    upload = reserve_replace(engine, principal, file_mfid, body, key, if_match, config, backfill=backfill)
    if isinstance(upload, MultipartStatus):
        return upload
    return start(engine, storage, upload)


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
        current = session.exec(select(UploadSession).where(UploadSession.upload_id == upload.upload_id).with_for_update()).one_or_none()
        if current is not None:
            current.cleaned_at = now()
            save(session, current)
            session.commit()


def fail(engine, storage, upload, code):
    logger.warning("multipart_failed upload_id=%s code=%s", upload.upload_id, code)
    with Session(engine) as session:
        current = session.exec(select(UploadSession).where(UploadSession.upload_id == upload.upload_id).with_for_update()).one_or_none()
        if current is not None and current.state == "completed":
            return current.state
        if current is not None and current.state not in TERMINAL:
            current.state = "failed"
            current.failure_code = code
            save(session, current)
            session.commit()
        state = current.state if current is not None else None
    try:
        cleanup(engine, storage, upload)
    except StorageFailure:
        logger.warning("multipart_cleanup_deferred upload_id=%s", upload.upload_id)
    return state


def complete(engine, storage, principal, dsid, body, retire=None):
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
    previous = None
    with Session(engine) as session:
        dataset = session.exec(select(Dataset).where(Dataset.unique_id == dsid).with_for_update()).one_or_none()
        current = session.exec(select(UploadSession).where(UploadSession.upload_id == upload.upload_id).with_for_update()).one_or_none()
        if current is None:
            code = "dataset_deleted"
        else:
            if current.state == "completed":
                return file_read(session, current)
            if current.state != "finalizing":
                raise HTTPException(409, "Upload was aborted during completion")
            file = None
            if dataset is None:
                code = "dataset_deleted"
            elif get_active_deletion_request(session, dsid) is not None:
                code = "dataset_pending_deletion"
            elif current.replace_file_mfid:
                try:
                    require_permission(session, demoted(session, principal), dsid, Permission.EDIT)
                except HTTPException:
                    code = "permission_revoked"
                else:
                    file = session.exec(select(AssociatedFile).where(AssociatedFile.mfid == current.replace_file_mfid).with_for_update()).one_or_none()
                    code = None if file is not None and file.storage_backend == "gcs" and file_etag(file.storage_path, file.storage_generation, file.sha256_hash, file.size) == current.replace_base_etag else "file_changed"
            else:
                authorize(session, principal, dsid)
                conflict = session.exec(select(AssociatedFile.id).where(AssociatedFile.dataset_id == dataset.id, AssociatedFile.filename == current.associated_file_name)).first()
                code = "filename_conflict" if conflict is not None else None
            if code is None:
                touch_resource_modification_time(session, dsid)
                if file is not None:
                    previous = (file.storage_path, file.storage_generation, file.filename, file.dataset_id)
                    file.storage_path = f"{current.bucket}/{current.gcs_path}"
                    file.storage_generation = metadata["generation"]
                    file.size = current.size
                    file.sha256_hash = current.claimed_sha256
                    file.storage_crc32c = current.claimed_crc32c
                    file.sha256_verified_at = None
                    session.add(file)
                else:
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
        raise HTTPException(403 if code == "permission_revoked" else 409, f"Upload could not be published: {code}")
    try:
        for provider_id in storage().uploads(upload):
            storage().abort(upload, provider_id)
    except StorageFailure:
        logger.warning("multipart_leftover_abort_deferred upload_id=%s", upload.upload_id)
    if previous is not None:
        retire_previous(engine, retire, result, dsid, previous)
    return result


def retire_previous(engine, retire, result, dsid, previous):
    path, generation, filename, dataset_id = previous
    retired = False
    try:
        if path:
            retired = retire_if_eligible(engine, retire, dsid, path, generation, filename, dataset_id)
    except Exception:
        logger.warning("file_retire_failed file=%s previous_path=%s", result.mfid, path, exc_info=True)
    logger.info("file_replaced file=%s dataset=%s previous_path=%s previous_generation=%s retired=%s", result.mfid, result.dataset_mfid, path, generation, retired)


def retire_if_eligible(engine, retire, dsid, path, generation, filename, dataset_id):
    bucket, _, key = path.partition("/")
    try:
        current = upload_bucket()
    except StorageNotConfigured:
        current = ""
    own_buckets = {current, os.environ.get("MULTIPART_UPLOAD_BUCKET", "").strip()} - {""}
    if retire is None or bucket not in own_buckets:
        return False
    try:
        resolve_storage_object(path, generation, dsid)
    except StorageLocationError:
        return False
    with Session(engine) as session:
        if session.exec(select(AssociatedFile.id).where(AssociatedFile.storage_path == path)).first() is not None:
            return False
        if generation is None:
            if key != f"{dsid}/{filename}":
                return False
            pathless = session.exec(select(AssociatedFile.filename).where(AssociatedFile.dataset_id == dataset_id, AssociatedFile.storage_path.is_(None))).all()
            if any(key.endswith(PurePosixPath(name).name) for name in pathless):
                return False
            pending = session.exec(select(UploadSession.expires_at).where(UploadSession.protocol.is_(None), UploadSession.dataset_mfid == dsid, UploadSession.gcs_path == key)).all()
            if any(utc(expires_at) >= now() for expires_at in pending):
                return False
    return retire(path, generation, now())


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

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from sqlmodel import Session

from app.api.dependencies import get_engine
from app.api.path_params import DatasetMfid
from app.models.datasets import AssociatedFileRead
from app.models.multipart import MultipartComplete, MultipartInitiate, MultipartParts, MultipartStatus, SignedPart, SignedParts, UploadedPart
from app.services import multipart as service
from app.services.multipart_storage import get_multipart_storage, StorageFailure, UploadMissing
from app.utils.auth_utils import authorize_user
from app.utils.multipart_config import MultipartStorageConfig
from app.utils.permissions import Principal

router = APIRouter(tags=["storage"])
engine = get_engine()


@router.post("/datasets/{dataset_mfid}/upload/multipart/initiate", response_model=MultipartStatus)
def initiate(auth_res: Annotated[Principal, Depends(authorize_user)], dsid: DatasetMfid, body: MultipartInitiate, idempotency_key: Annotated[str | None, Header(min_length=1, max_length=128)] = None):
    config = MultipartStorageConfig.from_env()
    if not config.enabled:
        raise HTTPException(503, "New multipart uploads are disabled", headers={"Retry-After": "60"})
    return service.initiate(engine, get_multipart_storage, auth_res, dsid, body, idempotency_key, config)


@router.post("/datasets/{dataset_mfid}/upload/multipart/{upload_id}/parts", response_model=SignedParts)
def sign_parts(auth_res: Annotated[Principal, Depends(authorize_user)], dsid: DatasetMfid, upload_id: str, body: MultipartParts):
    config = MultipartStorageConfig.from_env()
    with Session(engine) as session:
        upload = service.owned(session, dsid, upload_id, auth_res, lock=True)
        service.active(upload)
        if upload.state != "uploading":
            raise HTTPException(409, "Upload is not accepting parts")
        if upload.part_signing_after and service.utc(upload.part_signing_after) > service.now():
            raise HTTPException(429, "Part signing rate limit exceeded", headers={"Retry-After": str(config.signing_interval_seconds)})
        manifest = dict(upload.part_manifest or {})
        authorized = dict(manifest.get("authorized", {}))
        for part in body.parts:
            if part.part_number > upload.part_count:
                raise HTTPException(422, "Part number exceeds session geometry")
            previous = authorized.get(str(part.part_number))
            if previous is not None and previous != part.crc32c:
                raise HTTPException(409, "A part checksum cannot be changed")
            authorized[str(part.part_number)] = part.crc32c
        manifest["authorized"] = authorized
        upload.part_manifest = manifest
        upload.part_signing_after = service.now() + timedelta(seconds=config.signing_interval_seconds)
        service.save(session, upload)
        session.commit()
        session.refresh(upload)
        session.expunge(upload)
    expires_at = min(service.now() + timedelta(minutes=15), service.utc(upload.expires_at))

    def sign(part):
        size = service.part_size(upload, part.part_number)
        url, headers = storage.sign_part(upload, part.part_number, part.crc32c, size, expires_at)
        return SignedPart(part_number=part.part_number, url=url, headers=headers, offset=(part.part_number - 1) * upload.part_size, size=size, expires_at=expires_at)

    try:
        storage = get_multipart_storage()
        with ThreadPoolExecutor(max_workers=8) as pool:
            return SignedParts(parts=list(pool.map(sign, body.parts)))
    except Exception:
        raise HTTPException(503, "Part signing unavailable; retry with the same checksums") from None


@router.get("/datasets/{dataset_mfid}/upload/multipart/{upload_id}", response_model=MultipartStatus)
def get_status(auth_res: Annotated[Principal, Depends(authorize_user)], dsid: DatasetMfid, upload_id: str, part_number_marker: int = Query(default=0, ge=0, le=10000), limit: int = Query(default=1000, ge=1, le=1000)):
    with Session(engine) as session:
        upload = service.owned(session, dsid, upload_id, auth_res)
        result = service.status(session, upload)
        session.expunge(upload)
    if upload.state == "uploading" and not service.expired(upload):
        try:
            parts, marker = get_multipart_storage().list_parts(upload, part_number_marker, limit)
            result.parts = [UploadedPart(**part) for part in parts]
            result.next_part_number_marker = marker
        except UploadMissing:
            raise HTTPException(409, "Provider upload is unavailable; abort and start a new upload") from None
        except StorageFailure:
            raise HTTPException(503, "Upload progress is temporarily unavailable") from None
    return result


@router.post("/datasets/{dataset_mfid}/upload/multipart/complete", response_model=AssociatedFileRead)
def complete(auth_res: Annotated[Principal, Depends(authorize_user)], dsid: DatasetMfid, body: MultipartComplete):
    return service.complete(engine, get_multipart_storage, auth_res, dsid, body)


@router.delete("/datasets/{dataset_mfid}/upload/multipart/{upload_id}", status_code=204)
def abort(auth_res: Annotated[Principal, Depends(authorize_user)], dsid: DatasetMfid, upload_id: str):
    service.abort(engine, get_multipart_storage, auth_res, dsid, upload_id)
    return Response(status_code=204)

from typing import Optional

from sqlmodel import SQLModel

from app.models.datasets import AssociatedFileRead


class UploadInitiate(SQLModel):
    '''Body for POST /datasets/{dsid}/upload/initiate.'''
    filename: str
    size: int
    sha256_hash: Optional[str] = None


class UploadInitiateRead(SQLModel):
    '''Response from upload initiate — pass upload_id and resumable_uri to the client.
    If existing_file is set, a file with the same sha256_hash already exists for this
    dataset; skip the upload and /complete call and use existing_file directly.
    '''
    upload_id: Optional[str] = None
    resumable_uri: Optional[str] = None
    chunk_size_hint: Optional[int] = None
    existing_file: Optional[AssociatedFileRead] = None


class UploadComplete(SQLModel):
    '''Body for POST /datasets/{dsid}/upload/complete.'''
    upload_id: str
    sha256_hash: str        # computed by the client during upload


class UploadMultipartInitiateRead(SQLModel):
    '''Response from POST /datasets/{dsid}/upload/initiate/multipart.
    Either existing_file is set (dedup hit — no upload needed), or all other fields are set.
    Pass upload_id to POST /datasets/{dsid}/upload/complete after the transfer finishes.
    '''
    upload_id: Optional[str] = None
    access_token: Optional[str] = None
    token_expiry: Optional[str] = None   # ISO 8601
    bucket: Optional[str] = None
    object_prefix: Optional[str] = None  # "{dsid}/" — prefix the filename to get the full blob path
    project: Optional[str] = None
    existing_file: Optional[AssociatedFileRead] = None

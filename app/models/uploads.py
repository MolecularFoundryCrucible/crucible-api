from sqlmodel import SQLModel


class UploadInitiate(SQLModel):
    '''Body for POST /datasets/{dsid}/upload/initiate.'''
    filename: str
    size: int


class UploadInitiateRead(SQLModel):
    '''Response from upload initiate — pass upload_id and resumable_uri to the client.'''
    upload_id: str
    resumable_uri: str
    chunk_size_hint: int    # recommended chunk size in bytes (8 MiB)


class UploadComplete(SQLModel):
    '''Body for POST /datasets/{dsid}/upload/complete.'''
    upload_id: str
    sha256_hash: str        # computed by the client during upload

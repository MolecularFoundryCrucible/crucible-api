from typing import Optional

from pydantic import BaseModel


class StoreRead(BaseModel):
    '''A store files can be uploaded to. Endpoints, buckets and credentials stay on the server.'''
    id: str
    display_name: str
    description: Optional[str] = None
    default: bool

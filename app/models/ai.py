from typing import List, Literal, Optional
from pydantic import BaseModel

SUPPORTED_MEDIA_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


class ImageInput(BaseModel):
    data: str
    media_type: Literal["image/jpeg", "image/png", "image/gif", "image/webp"]


class ExtractMetadataRequest(BaseModel):
    images: List[ImageInput]
    context: Optional[str] = None

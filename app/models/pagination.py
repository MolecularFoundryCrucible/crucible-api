from typing import Generic, List, Optional, TypeVar
from pydantic import BaseModel

T = TypeVar("T")


class PaginatedResponse(BaseModel, Generic[T]):
    total: Optional[int] = None
    limit: int
    offset: Optional[int] = None
    items: List[T]
    next_cursor: Optional[str] = None

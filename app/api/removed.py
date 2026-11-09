"""Routes that were removed answer 410 Gone, naming their replacement, so older clients show the
reason instead of a bare 404."""
from typing import NoReturn

from fastapi import HTTPException

REMOVED_RESPONSE = {410: {"description": "This route was removed: `{\"detail\": {\"code\": \"route_removed\", \"message\": ...}}` names the replacement"}}


def removed(message: str) -> NoReturn:
    raise HTTPException(status_code=410, detail={"code": "route_removed", "message": message})

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session

from app.api.dependencies import get_engine
from app.models.query_params import ResourceGraphQuery
from app.models.resource_graph import ResourceGraphResponse
from app.services.resource_graph import build_resource_graph
from app.utils.auth_utils import authorize_user
from app.utils.permissions import Principal


router = APIRouter(tags=["resources"])


@router.get(
    "/resources/{resource_mfid}/graph",
    response_model=ResourceGraphResponse,
    responses={
        404: {
            "description": (
                "Root resource is missing, deleted, inaccessible, or not a sample or dataset."
            )
        }
    },
)
def read_resource_graph(
    resource_mfid: str,
    auth_res: Annotated[Principal, Depends(authorize_user)],
    params: Annotated[ResourceGraphQuery, Query()],
):
    """Return a bounded, authorization-filtered sample and dataset graph."""
    with Session(get_engine()) as session:
        graph = build_resource_graph(session, auth_res, resource_mfid, params)
        if graph is None:
            raise HTTPException(status_code=404, detail="Resource not found")
        return graph

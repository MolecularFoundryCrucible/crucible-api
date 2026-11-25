from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Response
from sqlmodel import Session

from app.api.dependencies import get_engine
from app.models.workers import WORKER_ID_PATTERN, WorkerKind, WorkerRead, WorkerRegister, WorkerRegistered
from app.services import workers
from app.utils.auth_utils import authorize_user
from app.utils.permissions import PlatformRole, Principal

router = APIRouter(tags=["workers"])
WorkerId = Annotated[str, Path(pattern=WORKER_ID_PATTERN)]


def _require_service_account(principal: Principal) -> None:
    if not principal.is_service_account:
        raise HTTPException(status_code=403, detail="Only service accounts register workers")


@router.put("/workers/{worker_id}", response_model=WorkerRegistered, responses={409: {"description": "A live worker registered by another account has this id: `{\"detail\": {\"code\": \"worker_id_taken\", \"worker_id\": ..., \"message\": ...}}`"}})
def register_worker(worker_id: WorkerId, body: WorkerRegister, auth_res: Annotated[Principal, Depends(authorize_user)]):
    '''Register a worker, or renew its registration: send it at startup and every `heartbeat_seconds`. It counts as live until `expires_at`. Requires a service account, of any platform role.'''
    _require_service_account(auth_res)
    return workers.register(get_engine(), auth_res, worker_id, body)


@router.delete("/workers/{worker_id}", status_code=204)
def deregister_worker(worker_id: WorkerId, auth_res: Annotated[Principal, Depends(authorize_user)]):
    '''Remove a worker's registration, as on a clean shutdown; succeeds when there is none. Requires a service account.'''
    _require_service_account(auth_res)
    workers.deregister(get_engine(), worker_id)
    return Response(status_code=204)


@router.get("/workers", response_model=list[WorkerRead])
def list_workers(auth_res: Annotated[Principal, Depends(authorize_user)], kind: Optional[WorkerKind] = None):
    '''Registered workers, live or seen in the last day, with what they can do and their versions. Requires a platform administrator or support.'''
    if not (auth_res.is_platform_admin or auth_res.platform_role is PlatformRole.SUPPORT):
        raise HTTPException(status_code=403, detail="Not authorized")
    with Session(get_engine()) as session:
        return workers.list_workers(session, kind)

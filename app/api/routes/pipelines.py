import hashlib
import json
import logging
from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import ValidationError
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.api.path_params import DatasetMfid
from app.config import get_secret
from app.general_utils import get_utc_datetime
from app.models.core_models import Dataset, PostprocessingRun
from app.models.pagination import PaginatedResponse
from app.models.pipelines import PipelineRead, PostprocessingRunRead, PostprocessingRunStatus, PostprocessingRunUpdate, PostprocessRequest
from app.services.pipelines import PIPELINES
from app.utils.auth_utils import authorize_user, permission_filter, require_permission
from app.utils.permissions import Permission, Principal
from app.utils.rmq_utils import send_pika_message
from app.utils.sql_utils import assert_not_pending_deletion, deletion_filter_for, select_active, select_readable

logger = logging.getLogger(__name__)
router = APIRouter(tags=["postprocessing"])
engine = get_engine()

FINISHED = {"completed", "failed"}


@router.get("/pipelines", response_model=List[PipelineRead])
def list_pipelines(auth_res: Annotated[Principal, Depends(authorize_user)]):
    '''List the pipelines that can postprocess a dataset, with the parameters each accepts.'''
    return [
        PipelineRead(name=pipeline.name, description=pipeline.description, parameters_schema=pipeline.parameters.model_json_schema())
        for pipeline in PIPELINES.values()
    ]


def _publish(run: PostprocessingRun, routing_key: str):
    send_pika_message(
        message=json.dumps({
            "run_id": run.id,
            "dataset_mfid": run.dataset_mfid,
            "dsid": run.dataset_mfid,
            "pipeline": run.pipeline,
            "parameters": run.parameters,
            "requested_by": run.requested_by,
        }),
        exchange="",
        routing_key=routing_key,
        user="admin",
        rmq_pw=get_secret("RABBITMQ_DEFAULT_PW"),
    )


QUEUE_FAILURE = "Could not queue the run"


def _queue(session, run: PostprocessingRun, routing_key: str) -> PostprocessingRun:
    try:
        _publish(run, routing_key)
    except Exception:
        logger.exception("pipeline_publish_failed run=%s pipeline=%s dataset=%s", run.id, run.pipeline, run.dataset_mfid)
        run.status = "failed"
        run.error = QUEUE_FAILURE
        run.finished_at = get_utc_datetime()
        session.add(run)
        session.commit()
        raise HTTPException(status_code=503, detail={"code": "pipeline_unavailable", "message": "Could not queue the run; try again later", "run_id": run.id}) from None
    logger.info("postprocessing_run_requested run=%s pipeline=%s dataset=%s", run.id, run.pipeline, run.dataset_mfid)
    return run


def _replay(session, run: PostprocessingRun, fingerprint: str, routing_key: str) -> PostprocessingRun:
    if run.request_fingerprint != fingerprint:
        raise HTTPException(status_code=409, detail={"code": "idempotency_key_reused", "message": "Idempotency key was already used for a different request", "run_id": run.id})
    if run.status == "failed" and run.error == QUEUE_FAILURE:
        run.status, run.error, run.finished_at = "requested", None, None
        session.add(run)
        session.commit()
        session.refresh(run)
        return _queue(session, run, routing_key)
    return run


def start_postprocessing_run(principal: Principal, dataset_mfid: str, pipeline_name: str, parameters: dict, idempotency_key: Optional[str] = None) -> PostprocessingRun:
    pipeline = PIPELINES.get(pipeline_name)
    if pipeline is None:
        raise HTTPException(status_code=422, detail={"code": "unknown_pipeline", "message": f"Unknown pipeline '{pipeline_name}'", "pipelines": sorted(PIPELINES)})
    try:
        parameters = pipeline.parameters.model_validate(parameters).model_dump(mode="json", exclude_none=True)
    except ValidationError as error:
        raise HTTPException(status_code=422, detail={"code": "invalid_parameters", "errors": json.loads(error.json(include_url=False))}) from None
    fingerprint = hashlib.sha256(json.dumps({"pipeline": pipeline.name, "parameters": parameters}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    with Session(engine) as session:
        dataset = session.exec(select_readable(Dataset, dataset_mfid, principal=principal)).one_or_none()
        if dataset is None:
            raise HTTPException(status_code=404, detail="Dataset not found")
        require_permission(session, principal, dataset.unique_id, Permission.CREATE)
        assert_not_pending_deletion(session, dataset.unique_id)

        def previous():
            return session.exec(select(PostprocessingRun).where(
                PostprocessingRun.requested_by == principal.user_id,
                PostprocessingRun.dataset_mfid == dataset.unique_id,
                PostprocessingRun.idempotency_key == idempotency_key,
            )).one_or_none()

        if idempotency_key is not None and (existing := previous()) is not None:
            return _replay(session, existing, fingerprint, pipeline.routing_key)
        run = PostprocessingRun(dataset_mfid=dataset.unique_id, pipeline=pipeline.name, parameters=parameters, requested_by=principal.user_id,
                                requested_at=get_utc_datetime(), idempotency_key=idempotency_key, request_fingerprint=fingerprint)
        session.add(run)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            existing = previous() if idempotency_key is not None else None
            if existing is None:
                raise
            return _replay(session, existing, fingerprint, pipeline.routing_key)
        session.refresh(run)
        return _queue(session, run, pipeline.routing_key)


@router.post("/datasets/{dataset_mfid}/postprocess", response_model=PostprocessingRunRead, status_code=202,
             responses={404: {"description": "Dataset not found or not readable"},
                        403: {"description": "Caller is below contributor on the dataset"},
                        409: {"description": "The dataset has an active deletion request (`pending_deletion`), or the Idempotency-Key was already used for a different pipeline or parameters (`idempotency_key_reused`)"},
                        422: {"description": "Unknown pipeline (`unknown_pipeline`) or invalid parameters (`invalid_parameters`)"},
                        503: {"description": "The run could not be queued (`pipeline_unavailable`); it is recorded as failed"}})
def postprocess_dataset(dataset_mfid: DatasetMfid, body: PostprocessRequest, auth_res: Annotated[Principal, Depends(authorize_user)],
                        idempotency_key: Annotated[Optional[str], Header(min_length=1, max_length=128, description="Retries with the same key return the original run instead of starting another.")] = None):
    '''Queue a postprocessing run on a dataset. Requires contributor access. Each request starts a new run unless it repeats an Idempotency-Key.'''
    return start_postprocessing_run(auth_res, dataset_mfid, body.pipeline, body.parameters, idempotency_key)


@router.get("/postprocessing_runs", response_model=PaginatedResponse[PostprocessingRunRead])
def list_postprocessing_runs(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    dataset_mfid: Optional[str] = None,
    pipeline: Optional[str] = None,
    status: Optional[PostprocessingRunStatus] = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    '''List postprocessing runs on datasets the caller can read, newest first.'''
    with Session(engine) as session:
        if dataset_mfid is not None and session.exec(select_active(Dataset, dataset_mfid, Dataset.id, principal=auth_res)).first() is None:
            raise HTTPException(status_code=404, detail="Dataset not found")
        query = (
            select(PostprocessingRun)
            .join(Dataset, Dataset.unique_id == PostprocessingRun.dataset_mfid)
            .where(permission_filter(auth_res, Dataset, Permission.READ))
            .where(deletion_filter_for(Dataset))
        )
        if dataset_mfid is not None:
            query = query.where(PostprocessingRun.dataset_mfid == dataset_mfid)
        if pipeline is not None:
            query = query.where(PostprocessingRun.pipeline == pipeline)
        if status is not None:
            query = query.where(PostprocessingRun.status == status)
        total = session.exec(select(func.count()).select_from(query.subquery())).one()
        items = session.exec(query.order_by(PostprocessingRun.id.desc()).offset(offset).limit(limit)).all()
        return PaginatedResponse(total=total, limit=limit, offset=offset, items=[PostprocessingRunRead.model_validate(item) for item in items])


def _run_with_access(session, principal, run_id, permission):
    run = session.get(PostprocessingRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Postprocessing run not found")
    if session.exec(select_readable(Dataset, run.dataset_mfid, Dataset.id, principal=principal)).first() is None:
        raise HTTPException(status_code=404, detail="Postprocessing run not found")
    if permission is not Permission.READ:
        require_permission(session, principal, run.dataset_mfid, permission)
    return run


@router.get("/postprocessing_runs/{run_id}", response_model=PostprocessingRunRead)
def get_postprocessing_run(run_id: int, auth_res: Annotated[Principal, Depends(authorize_user)]):
    '''Return one postprocessing run. Requires read access to its dataset.'''
    with Session(engine) as session:
        return _run_with_access(session, auth_res, run_id, Permission.READ)


@router.patch("/postprocessing_runs/{run_id}", response_model=PostprocessingRunRead,
              responses={409: {"description": "The run already finished (`run_finished`)"}})
def update_postprocessing_run(run_id: int, body: PostprocessingRunUpdate, auth_res: Annotated[Principal, Depends(authorize_user)]):
    '''Report a run's progress: running, completed, or failed with an error. Requires edit access to its dataset.'''
    with Session(engine) as session:
        run = _run_with_access(session, auth_res, run_id, Permission.EDIT)
        if run.status in FINISHED:
            raise HTTPException(status_code=409, detail={"code": "run_finished", "message": f"Run already {run.status}"})
        now = get_utc_datetime()
        run.status = body.status
        run.error = body.error if body.status == "failed" else None
        if run.started_at is None:
            run.started_at = now
        if body.status in FINISHED:
            run.finished_at = now
        session.add(run)
        session.commit()
        session.refresh(run)
        return run

import time
from typing import Annotated, List

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.build_info import api_version, branch_name, git_commit
from app.models.core_models import Keyword
from app.models.health import LivenessResponse, ReadinessResponse
from app.utils.auth_utils import authorize_user

router = APIRouter(tags=["misc"])
engine = get_engine()


def _check_db():
    t0 = time.monotonic()
    try:
        with Session(engine) as session:
            revisions = session.execute(
                text("SELECT version_num FROM alembic_version ORDER BY version_num")
            ).scalars().all()
        return "ok", round((time.monotonic() - t0) * 1000, 1), sorted(revisions)
    except Exception:
        return "error", None, []


@router.get("/health/live", response_model=LivenessResponse)
def liveness():
    """Liveness probe. Returns 200 when the process is running."""
    return {"status": "ok"}


@router.get(
    "/health/ready",
    response_model=ReadinessResponse,
    responses={503: {"model": ReadinessResponse}},
)
def readiness():
    """Readiness probe with database, schema, and build provenance."""
    db_status, db_ms, schema_revisions = _check_db()
    ok = db_status == "ok"
    return JSONResponse(
        status_code=200 if ok else 503,
        content={
            "status": "ok" if ok else "degraded",
            "build": {
                "api_version": api_version(),
                "git_commit": git_commit(),
                "branch": branch_name(),
            },
            "database": {
                "status": db_status,
                "latency_ms": db_ms,
                "schema_revisions": schema_revisions,
            },
        },
    )


@router.get(
    "/health",
    response_model=ReadinessResponse,
    responses={503: {"model": ReadinessResponse}},
)
def health():
    """Compatibility alias for /health/ready."""
    return readiness()


@router.get("/keywords", response_model=List[Keyword])
def list_all_keywords():
    '''
    Returns a list of all keywords in the database.
    Each keyword includes the num_datasets field showing how many datasets use that keyword.
    '''
    with Session(engine) as session:
        keywords = session.exec(select(Keyword)).all()
    return keywords


@router.get("/keywords/{keyword}", response_model=List[Keyword])
def read_keyword(keyword: str, permissive: bool = True):
    '''
    Returns a list of all keyword entries that match the provided keyword.
    If permissive is set to False, then all returned entries will be an exact match to the provided string.
    Otherwise, keywords will be searched as case insensitive.  The default configuration has permissive set to True.
    '''
    if permissive:
        cmd = select(Keyword).where(Keyword.keyword.ilike(f"{keyword.lower()}"))
    else:
        cmd = select(Keyword).where(Keyword.keyword == keyword)

    with Session(engine) as session:
        K = session.exec(cmd).all()
    return(K)

# TODO: We can find a way to pull this more dynamically, but for now make the list API available
@router.get("/ingestors", response_model=List[str])
def list_ingestors():
    '''
    Returns a list of all ingestor options to use when making an ingestion request.
    '''
    ingestors =['AFMIngestor',
                'PtychographyH5Ingestor',
                'SimpleTiledImageScopeFoundryH5Ingestor,' 
                'BioGlowIngestor',
                'QSpleemSVRampIngestor',
                'QSpleemSVRampSpinIngestor',
                'QSpleemImageIngestor',
                'QSpleemSPLEEMImageIngestor',
                'QSpleemDepositionMonitorIngestor',
                'QSpleemARRESEKIngestor',
                'QSpleemARRESMMIngestor',
                'RgaTeyBatchIngestor',
                'CanonCaptureScopeFoundryH5Ingestor,' 
                'SingleSpecScopeFoundryH5Ingestor',
                'HyperspecScopeFoundryH5Ingestor',
                'HyperspecSweepScopeFoundryH5Ingestor',
                'ToupcamLiveScopeFoundryH5Ingestor',
                'CLSyncRasterScanIngestor',
                'CLHyperspecIngestor,' 
                'SpinbotSpecLineIngestor',
                'SpinbotCameraCaptureIngestor,' 
                'SpinbotPhotoRunIngestor,' 
                'InSituPlIngestor',
                'InorganicXRDIngestor',
                'CziIngestor',
                'DigitalMicrographIngestor',
                'EmiIngestor',
                'SerIngestor',
                'BcfIngestor',
                'BerkeleyEmdIngestor',
                'VeloxEmdIngestor',
                'SpinbotSpecRunIngestor',
                'ImageIngestor',
                'NirvanaMultiPosLineScanIngestor',
                'NirvanaMultiPosSpecRunIngestor',
                'ScopeFoundryH5Ingestor',
                'H5Ingestor']
    
    return ingestors

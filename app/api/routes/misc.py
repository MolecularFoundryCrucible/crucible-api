import time
from typing import Annotated, List

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.models.core_models import Keyword
from app.utils.auth_utils import authorize_user

router = APIRouter(tags=["misc"])
engine = get_engine()


def _api_version():
    try:
        from importlib.metadata import version
        return version("crucible-api")
    except Exception:
        return None


def _check_db():
    """Returns (status, latency_ms). status is 'ok' or 'error'."""
    t0 = time.monotonic()
    try:
        with Session(engine) as session:
            session.execute(text("SELECT 1"))
        return "ok", round((time.monotonic() - t0) * 1000, 1)
    except Exception:
        return "error", None


@router.get("/health/live")
def liveness():
    """Liveness probe — no auth, no DB check. Returns 200 if the process is running."""
    return {"status": "ok"}


@router.get("/health/ready")
def readiness():
    """
    Readiness probe — no auth. Checks DB connectivity and returns 503 if unreachable.
    Includes DB response latency in ms.
    """
    db_status, db_ms = _check_db()
    ok = db_status == "ok"
    return JSONResponse(
        status_code=200 if ok else 503,
        content={
            "status": "ok" if ok else "degraded",
            "db": db_status,
            "db_ms": db_ms,
            "version": _api_version(),
        },
    )


@router.get("/health")
def health():
    """Alias for /health/ready — kept for backwards compatibility."""
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
                'SimpleTiledImageScopeFoundryH5Ingestor',
                'BioGlowIngestor',
                'QSpleemSVRampIngestor',
                'QSpleemSVRampSpinIngestor',
                'QSpleemImageIngestor',
                'QSpleemSPLEEMImageIngestor',
                'QSpleemDepositionMonitorIngestor',
                'QSpleemARRESEKIngestor',
                'QSpleemARRESMMIngestor',
                'RgaTeyBatchIngestor',
                'CanonCaptureScopeFoundryH5Ingestor', 
                'SingleSpecScopeFoundryH5Ingestor',
                'HyperspecScopeFoundryH5Ingestor',
                'HyperspecSweepScopeFoundryH5Ingestor',
                'ToupcamLiveScopeFoundryH5Ingestor',
                'CLSyncRasterScanIngestor',
                'CLHyperspecIngestor',
                'SpinbotSpecLineIngestor',
                'SpinbotCameraCaptureIngestor',
                'SpinbotPhotoRunIngestor', 
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



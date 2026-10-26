import logging
import os
import time
from functools import lru_cache
from pathlib import Path
from typing import Annotated, List

from fastapi import APIRouter, Depends, HTTPException, Query
from alembic.script import ScriptDirectory
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.build_info import api_version, branch_name, git_commit
from app.models.core_models import Keyword
from app.models.health import LivenessResponse, ReadinessResponse
from app.utils.auth_utils import authorize_user

router = APIRouter(tags=["misc"])
logger = logging.getLogger(__name__)
MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"


def _check_db():
    t0 = time.monotonic()
    try:
        with Session(get_engine()) as session:
            revisions = session.execute(
                text("SELECT version_num FROM alembic_version ORDER BY version_num")
            ).scalars().all()
        return "ok", round((time.monotonic() - t0) * 1000, 1), sorted(revisions)
    except Exception:
        return "error", None, []


@lru_cache(maxsize=1)
def _migration_scripts():
    try:
        return ScriptDirectory(str(MIGRATIONS_DIR))
    except Exception:
        logger.warning("Migration scripts unavailable at %s; readiness requires an exact schema match", MIGRATIONS_DIR)
        return None


def _known_revision(scripts, revision):
    try:
        return scripts.get_revision(revision) is not None
    except Exception:
        return False


def _schema_state(current, expected):
    """current when the database matches, ahead when it carries migrations newer than this build,
    behind when a migration this build needs is missing, mismatch when it cannot be told apart."""
    if not expected or current == expected:
        return "current"
    scripts = _migration_scripts()
    if scripts is None or not current:
        return "mismatch" if current else "behind"
    heads = set(scripts.get_heads())
    state = "current"
    for wanted in expected:
        if not _known_revision(scripts, wanted):
            return "mismatch"
        reached = False
        for revision in current:
            if not _known_revision(scripts, revision):
                if wanted in heads:
                    reached, state = True, "ahead"
            elif wanted in {script.revision for script in scripts.iterate_revisions(revision, "base")}:
                reached = True
                if revision != wanted:
                    state = "ahead"
        if not reached:
            return "behind" if all(_known_revision(scripts, revision) for revision in current) else "mismatch"
    return state


def _expected_schema_revisions():
    configured = os.getenv("EXPECTED_DB_REVISIONS", "")
    return sorted(revision.strip() for revision in configured.split(",") if revision.strip())


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
    """Readiness probe with database, schema, and build provenance. Ready while the database schema is current or ahead of this build (newer additive migrations applied before a deploy); not ready when it is behind."""
    db_status, db_ms, schema_revisions = _check_db()
    expected_revisions = _expected_schema_revisions()
    schema_state = _schema_state(schema_revisions, expected_revisions) if db_status == "ok" else None
    ok = db_status == "ok" and schema_state in ("current", "ahead")
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
                "status": "ok" if ok else "error",
                "latency_ms": db_ms,
                "schema_revisions": schema_revisions,
                "schema_state": schema_state,
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
    with Session(get_engine()) as session:
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

    with Session(get_engine()) as session:
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

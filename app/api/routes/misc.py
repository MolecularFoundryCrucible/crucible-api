import time
from typing import Annotated, List, Union

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import desc, func, join, text, Text
from sqlalchemy.sql import and_, or_
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.models.core_models import Dataset, DatasetLink, DatasetSampleLink, Instrument, Project, ResourceACL, DeletionRequest, Keyword, ScientificMetadataBinary, Resource, Sample, SampleLink, User
from app.models.datasets import DatasetRead, DatasetResponse
from app.models.instrument import InstrumentResponse
from app.models.links import LinkedResource
from app.models.projects import ProjectWithLead
from app.models.samples import SampleResponse
from app.utils.auth_utils import authorize_user

router = APIRouter(tags=["misc"])
engine = get_engine()


def _fetch_links(session, resource_type: str, entity) -> List[LinkedResource]:
    results: List[LinkedResource] = []
    if resource_type == "dataset":
        parents = session.exec(
            select(Dataset).join(DatasetLink, DatasetLink.parent_dataset_id == Dataset.id)
            .where(DatasetLink.child_dataset_id == entity.id)
        ).all()
        results += [LinkedResource(unique_id=d.unique_id, resource_type="dataset", name=d.dataset_name, relationship="parent") for d in parents]

        children = session.exec(
            select(Dataset).join(DatasetLink, DatasetLink.child_dataset_id == Dataset.id)
            .where(DatasetLink.parent_dataset_id == entity.id)
        ).all()
        results += [LinkedResource(unique_id=d.unique_id, resource_type="dataset", name=d.dataset_name, relationship="child") for d in children]

        samples = session.exec(
            select(Sample).join(DatasetSampleLink, DatasetSampleLink.sample_id == Sample.id)
            .where(DatasetSampleLink.dataset_id == entity.id)
        ).all()
        results += [LinkedResource(unique_id=s.unique_id, resource_type="sample", name=s.sample_name, relationship="associated") for s in samples]

    elif resource_type == "sample":
        parents = session.exec(
            select(Sample).join(SampleLink, SampleLink.parent_sample_id == Sample.id)
            .where(SampleLink.sample_id == entity.id)
        ).all()
        results += [LinkedResource(unique_id=s.unique_id, resource_type="sample", name=s.sample_name, relationship="parent") for s in parents]

        children = session.exec(
            select(Sample).join(SampleLink, SampleLink.sample_id == Sample.id)
            .where(SampleLink.parent_sample_id == entity.id)
        ).all()
        results += [LinkedResource(unique_id=s.unique_id, resource_type="sample", name=s.sample_name, relationship="child") for s in children]

        datasets = session.exec(
            select(Dataset).join(DatasetSampleLink, DatasetSampleLink.dataset_id == Dataset.id)
            .where(DatasetSampleLink.sample_id == entity.id)
        ).all()
        results += [LinkedResource(unique_id=d.unique_id, resource_type="dataset", name=d.dataset_name, relationship="associated") for d in datasets]

    return results

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


@router.get("/scientific_metadata/search", response_model=List[ScientificMetadataBinary])
def search_scientific_metadata(auth_res: Annotated[tuple, Depends(authorize_user)], q: str, limit: int = Query(default=50, ge=1, le=200)):
    """
    Perform a ranked Full-Text Search (FTS) on scientific metadata using PostgreSQL's tsvector, 
    tsquery, and ts_rank_cd. Default limit is 50 results, max is 200.
    """
    user_unique_id, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    search_query = func.plainto_tsquery('english', q)
    metadata_vector = func.to_tsvector(
        'english',
        func.jsonb_path_query_array(ScientificMetadataBinary.scientific_metadata, text("'$[*]'")).cast(Text)
    )

    rank_expr = func.ts_rank_cd(metadata_vector, search_query)

    stmt = (
        select(ScientificMetadataBinary, rank_expr.label("rank"))
        .join(Dataset, ScientificMetadataBinary.dataset_id == Dataset.id)
        .join(ResourceACL, ResourceACL.mfid == Dataset.unique_id, isouter=True)
        .where(
            and_(
                or_(ResourceACL.accessgroup_id.in_(ag_ids), Dataset.public == True),
                metadata_vector.op('@@')(search_query)
            )
        )
        .order_by(desc("rank"))
        .distinct()
        .limit(limit)
    )
    with Session(engine) as session:
        results = session.exec(stmt).all()
        return [row[0] for row in results]



@router.get("/resources/{resource_id}", response_model=Union[DatasetResponse, SampleResponse, InstrumentResponse, ProjectWithLead])
def get_resource(auth_res: Annotated[tuple, Depends(authorize_user)], resource_id: str,
                 include_metadata: bool = False, include_links: bool = False):
    """
    Fetch a dataset, sample, instrument, or project by ID without knowing its type in advance.
    Resolves the type from idtype, then returns the full resource record.
    Pass include_metadata=true to include scientific metadata.
    Pass include_links=true to include parent/child/associated resources (datasets and samples only).
    """
    user_unique_id, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        resource = session.exec(
            select(Resource).where(Resource.mfid == resource_id)
        ).one_or_none()
        if resource is None:
            raise HTTPException(status_code=404, detail="Resource not found")

        deletion_req = session.exec(
            select(DeletionRequest)
            .where(DeletionRequest.resource_id == resource_id)
            .where(DeletionRequest.status.in_(["pending", "approved"]))
        ).one_or_none()
        deletion_req_dict = deletion_req.model_dump() if deletion_req else None

        sci_md = None
        if include_metadata:
            smb = session.exec(
                select(ScientificMetadataBinary).where(ScientificMetadataBinary.unique_id == resource_id)
            ).one_or_none()
            sci_md = smb.scientific_metadata if smb else None

        if resource.resource_type == "dataset":
            ds = session.exec(
                select(Dataset)
                .select_from(join(Dataset, ResourceACL, ResourceACL.mfid == Dataset.unique_id, isouter=True))
                .where(Dataset.unique_id == resource_id)
                .filter((ResourceACL.accessgroup_id.in_(ag_ids)) | (Dataset.public == True))
                .distinct()
            ).one_or_none()
            if ds is None:
                raise HTTPException(status_code=404, detail="Dataset not found or access denied")
            return DatasetResponse.model_validate({
                **ds.model_dump(),
                "creation_time": resource.creation_time,
                "modification_time": resource.modification_time,
                "scientific_metadata": sci_md,
                "deletion_request": deletion_req_dict,
                "links": _fetch_links(session, "dataset", ds) if include_links else None,
            })

        elif resource.resource_type == "sample":
            samp = session.exec(
                select(Sample)
                .options(selectinload(Sample.datasets))
                .where(Sample.unique_id == resource_id)
            ).one_or_none()
            if samp is None:
                raise HTTPException(status_code=404, detail="Sample not found")
            acl_ok = session.exec(
                select(ResourceACL)
                .where(ResourceACL.mfid == resource_id)
                .where(ResourceACL.accessgroup_id.in_(ag_ids))
            ).first()
            if acl_ok is None:
                raise HTTPException(status_code=403, detail="Access denied")
            return SampleResponse.model_validate({
                **samp.model_dump(),
                "datasets": [DatasetRead.model_validate(ds) for ds in samp.datasets],
                "creation_time": resource.creation_time,
                "modification_time": resource.modification_time,
                "scientific_metadata": sci_md,
                "deletion_request": deletion_req_dict,
                "links": _fetch_links(session, "sample", samp) if include_links else None,
            })

        elif resource.resource_type == "instrument":
            inst = session.exec(
                select(Instrument).where(Instrument.unique_id == resource_id)
            ).one_or_none()
            if inst is None:
                raise HTTPException(status_code=404, detail="Instrument not found")
            return InstrumentResponse.model_validate({
                **inst.model_dump(),
                "creation_time": resource.creation_time,
                "modification_time": resource.modification_time,
                "scientific_metadata": sci_md,
            })

        elif resource.resource_type == "project":
            proj = session.exec(
                select(Project).where(Project.project_id == resource_id)
            ).one_or_none()
            if proj is None:
                raise HTTPException(status_code=404, detail="Project not found")
            lead = None
            if proj.project_lead_orcid:
                lead = session.exec(
                    select(User).where(User.unique_id == proj.project_lead_orcid)
                ).one_or_none()
            return ProjectWithLead(
                **proj.model_dump(),
                lead=lead,
                scientific_metadata=sci_md,
            )

        else:
            raise HTTPException(status_code=404, detail="Resource not found")


@router.get("/resources/{resource_id}/links", response_model=List[LinkedResource])
def get_linked_resources(resource_id: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    """
    Returns the immediate links for a resource (dataset or sample):
    - parent/child via lineage (DatasetLink or SampleLink)
    - associated resources (DatasetSampleLink)
    """
    _, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        resource = session.exec(
            select(Resource).where(Resource.mfid == resource_id)
        ).one_or_none()
        if resource is None:
            raise HTTPException(status_code=404, detail="Resource not found")

        if resource.resource_type != "instrument":
            acl_ok = session.exec(
                select(ResourceACL)
                .where(ResourceACL.mfid == resource_id)
                .where(ResourceACL.accessgroup_id.in_(ag_ids))
            ).first()
            is_public = (
                resource.resource_type == "dataset" and
                session.exec(select(Dataset.public).where(Dataset.unique_id == resource_id)).one_or_none()
            )
            if acl_ok is None and not is_public:
                raise HTTPException(status_code=403, detail="Access denied")

        if resource.resource_type == "dataset":
            entity = session.exec(select(Dataset).where(Dataset.unique_id == resource_id)).one_or_none()
            if entity is None:
                raise HTTPException(status_code=404, detail="Dataset not found")
        elif resource.resource_type == "sample":
            entity = session.exec(select(Sample).where(Sample.unique_id == resource_id)).one_or_none()
            if entity is None:
                raise HTTPException(status_code=404, detail="Sample not found")
        else:
            raise HTTPException(status_code=400, detail=f"Links not supported for resource type '{resource.resource_type}'")

        return _fetch_links(session, resource.resource_type, entity)

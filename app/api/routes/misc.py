from typing import Annotated, List, Union

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import desc, func, join, text, Text
from sqlalchemy.sql import and_, or_
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.models.core_models import Dataset, ResourceACL, DeletionRequest, Keyword, NewScientificMetadata, Resource, Sample
from app.models.datasets import DatasetRead, DatasetResponse
from app.models.samples import SampleResponse
from app.utils.auth_utils import authorize_user

router = APIRouter(tags=["misc"])
engine = get_engine()

@router.get("/health")
def health():
    """
    Health check — no authentication required.
    Returns 200 when the API and database are reachable, 503 if the DB is down.
    """
    try:
        with Session(engine) as session:
            session.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception:
        db_status = "error"

    try:
        from importlib.metadata import version
        api_version = version("crucible-api")
    except Exception:
        api_version = None

    ok = db_status == "ok"
    return JSONResponse(
        status_code=200 if ok else 503,
        content={"status": "ok" if ok else "degraded", "db": db_status, "version": api_version},
    )


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


@router.get("/scientific_metadata/search", response_model=List[NewScientificMetadata])
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
        func.jsonb_path_query_array(NewScientificMetadata.scientific_metadata, text("'$[*]'")).cast(Text)
    )

    rank_expr = func.ts_rank_cd(metadata_vector, search_query)

    stmt = (
        select(NewScientificMetadata, rank_expr.label("rank"))
        .join(Dataset, NewScientificMetadata.dataset_id == Dataset.id)
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


@router.get("/idtype/{unique_id}")
def get_object_type(auth_res: Annotated[tuple, Depends(authorize_user)], unique_id: str):
    with Session(engine) as session:
        object_type = session.exec(select(Resource).where(Resource.mfid == unique_id)).one_or_none()
        if object_type is None:
            raise HTTPException(status_code=404, detail="MFID not found")
        else:
            return object_type


@router.get("/resources/{resource_id}", response_model=Union[DatasetResponse, SampleResponse], response_model_exclude_none=True)
def get_resource(auth_res: Annotated[tuple, Depends(authorize_user)], resource_id: str):
    """
    Fetch a dataset or sample by ID without knowing its type in advance.
    Resolves the type from idtype, then returns the full resource record.
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

        if resource.resource_type == "dataset":
            ds = session.exec(
                select(Dataset)
                .options(selectinload(Dataset.resource))
                .select_from(join(Dataset, ResourceACL, ResourceACL.mfid == Dataset.unique_id, isouter=True))
                .where(Dataset.unique_id == resource_id)
                .filter((ResourceACL.accessgroup_id.in_(ag_ids)) | (Dataset.public == True))
                .distinct()
            ).one_or_none()
            if ds is None:
                raise HTTPException(status_code=404, detail="Dataset not found or access denied")
            return DatasetResponse.model_validate({
                **ds.model_dump(),
                "creation_time": ds.resource.creation_time if ds.resource else None,
                "modification_time": ds.resource.modification_time if ds.resource else None,
                "deletion_request": deletion_req_dict,
            })

        elif resource.resource_type == "sample":
            samp = session.exec(
                select(Sample)
                .options(selectinload(Sample.datasets), selectinload(Sample.resource))
                .where(Sample.unique_id == resource_id)
            ).one_or_none()
            if samp is None:
                raise HTTPException(status_code=404, detail="Sample not found")
            return SampleResponse.model_validate({
                **samp.model_dump(),
                "datasets": [DatasetRead.model_validate(ds) for ds in samp.datasets],
                "creation_time": samp.resource.creation_time if samp.resource else None,
                "modification_time": samp.resource.modification_time if samp.resource else None,
                "deletion_request": deletion_req_dict,
            })

        else:
            raise HTTPException(status_code=400, detail=f"Unsupported resource type: {resource.resource_type}")

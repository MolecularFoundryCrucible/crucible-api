from typing import Annotated, List, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, func, text, Text
from sqlalchemy.orm import attributes, selectinload
from sqlalchemy.sql import and_, exists, or_
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.general_utils import get_tz_isoformat
from app.models.core_models import (
    Dataset, DatasetLink, DatasetSampleLink,
    DeletionRequest,
    Instrument, Project, Resource,
    ResourceACL, Sample, SampleLink,
    ScientificMetadata, ScientificMetadataBinary, User,
)
from app.models.datasets import DatasetRead, DatasetResponse, ScientificMetadataBinaryRead
from app.models.deletion import DeletionRequestRead
from app.models.instrument import InstrumentResponse
from app.models.links import LinkedResource
from app.models.projects import ProjectWithLead
from app.models.samples import SampleResponse
from app.utils.auth_utils import authorize_user, require_read_access, require_write_access
from app.utils.sql_utils import add_or_update


router = APIRouter(tags=["resources"])
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


def _dual_write_old_table(session, unique_id: str, scientific_md: dict):
    """Write to the legacy scientific_metadata table for dataset resources (v1 compatibility)."""
    is_dataset = session.exec(
        select(Dataset).where(Dataset.unique_id == unique_id)
    ).one_or_none()
    if is_dataset:
        md_obj = add_or_update(
            session,
            ScientificMetadata,
            where=(ScientificMetadata.dataset_unique_id == unique_id),
            dataset_unique_id=unique_id,
            scientific_metadata=scientific_md,
        )
        attributes.flag_modified(md_obj, 'scientific_metadata')


@router.get("/resources/{resource_id}", response_model=Union[DatasetResponse, SampleResponse, InstrumentResponse, ProjectWithLead])
def get_resource(auth_res: Annotated[tuple, Depends(authorize_user)], resource_id: str,
                 include_metadata: bool = False, include_links: bool = False):
    """
    Fetch a dataset, sample, instrument, or project by ID without knowing its type in advance.
    Resolves the type from idtype, then returns the full resource record.
    Pass include_metadata=true to include scientific metadata.
    Pass include_links=true to include parent/child/associated resources (datasets and samples only).
    """
    _, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        resource = session.exec(
            select(Resource).where(Resource.mfid == resource_id)
        ).one_or_none()
        if resource is None:
            raise HTTPException(status_code=404, detail="Resource not found")

        require_read_access(session, resource_id, ag_ids)

        sci_md = None
        if include_metadata:
            smb = session.exec(
                select(ScientificMetadataBinary).where(ScientificMetadataBinary.unique_id == resource_id)
            ).one_or_none()
            sci_md = smb.scientific_metadata if smb else None

        if resource.resource_type == "dataset":
            ds = session.exec(
                select(Dataset).where(Dataset.unique_id == resource_id)
            ).one_or_none()
            if ds is None:
                raise HTTPException(status_code=404, detail="Dataset not found")
            return DatasetResponse.model_validate({
                **ds.model_dump(),
                "public": resource.public,
                "creation_time": resource.creation_time,
                "modification_time": resource.modification_time,
                "scientific_metadata": sci_md,
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
            return SampleResponse.model_validate({
                **samp.model_dump(),
                "public": resource.public,
                "datasets": [DatasetRead.model_validate(ds) for ds in samp.datasets],
                "creation_time": resource.creation_time,
                "modification_time": resource.modification_time,
                "scientific_metadata": sci_md,
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
                creation_time=resource.creation_time,
                modification_time=resource.modification_time,
            )

        else:
            raise HTTPException(status_code=404, detail="Resource not found")


@router.get("/resources/{resource_id}/links", response_model=List[LinkedResource])
def get_resource_links(resource_id: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    """
    Returns the immediate links for a resource (dataset or sample):
    parent/child via lineage and associated resources.
    """
    _, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        resource = session.exec(
            select(Resource).where(Resource.mfid == resource_id)
        ).one_or_none()
        if resource is None:
            raise HTTPException(status_code=404, detail="Resource not found")

        require_read_access(session, resource_id, ag_ids)

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


@router.get("/resources/{unique_id}/metadata", response_model=ScientificMetadataBinaryRead)
def read_resource_metadata(auth_res: Annotated[tuple, Depends(authorize_user)], unique_id: str):
    _, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]
    with Session(engine) as session:
        resource = session.exec(
            select(Resource)
            .options(selectinload(Resource.scientific_metadata))
            .where(Resource.mfid == unique_id)
        ).one_or_none()

        if resource is None:
            raise HTTPException(status_code=404, detail="Resource not found")

        require_read_access(session, unique_id, ag_ids)

        return resource.scientific_metadata


@router.post("/resources/{unique_id}/metadata", response_model=ScientificMetadataBinaryRead)
def create_resource_metadata(auth_res: Annotated[tuple, Depends(authorize_user)], unique_id: str, scientific_md: dict,
                             overwrite: bool = False):
    _, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        resource = session.exec(
            select(Resource).where(Resource.mfid == unique_id)
        ).one_or_none()

        if resource is None:
            raise HTTPException(status_code=404, detail="Resource not found")

        require_write_access(session, ag_ids, [unique_id])

        if not overwrite:
            existing = session.exec(
                select(ScientificMetadataBinary).where(ScientificMetadataBinary.unique_id == unique_id)
            ).one_or_none()
            if existing and existing.scientific_metadata:
                raise HTTPException(
                    status_code=409,
                    detail="Metadata already exists. Use overwrite=true to replace it, or PATCH to merge."
                )

        new_md_obj = add_or_update(
            session,
            ScientificMetadataBinary,
            where=(ScientificMetadataBinary.unique_id == unique_id),
            unique_id=unique_id,
            scientific_metadata=scientific_md,
        )
        attributes.flag_modified(new_md_obj, 'scientific_metadata')
        resource.modification_time = get_tz_isoformat()

        _dual_write_old_table(session, unique_id, scientific_md)

        session.commit()
        session.refresh(new_md_obj)
        return new_md_obj


@router.patch("/resources/{unique_id}/metadata", response_model=ScientificMetadataBinaryRead)
def update_resource_metadata(auth_res: Annotated[tuple, Depends(authorize_user)], unique_id: str, scientific_md_updates: dict):
    _, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        resource = session.exec(
            select(Resource).where(Resource.mfid == unique_id)
        ).one_or_none()

        if resource is None:
            raise HTTPException(status_code=404, detail="Resource not found")

        require_write_access(session, ag_ids, [unique_id])

        stored_md = session.exec(
            select(ScientificMetadataBinary).where(ScientificMetadataBinary.unique_id == unique_id)
        ).one_or_none()
        md_dict = {**(stored_md.scientific_metadata if stored_md else {}), **scientific_md_updates}

        new_md_obj = add_or_update(
            session,
            ScientificMetadataBinary,
            where=(ScientificMetadataBinary.unique_id == unique_id),
            unique_id=unique_id,
            scientific_metadata=md_dict,
        )
        attributes.flag_modified(new_md_obj, 'scientific_metadata')
        resource.modification_time = get_tz_isoformat()

        _dual_write_old_table(session, unique_id, md_dict)

        session.commit()
        session.refresh(new_md_obj)
        return new_md_obj


@router.get("/resources/metadata/search", response_model=List[ScientificMetadataBinaryRead])
def search_resource_metadata(auth_res: Annotated[tuple, Depends(authorize_user)], q: str,
                             limit: int = Query(default=50, ge=1, le=200)):
    """
    Ranked full-text search on scientific metadata using PostgreSQL tsvector/tsquery.
    Returns results the user has read access to, ordered by relevance.
    """
    _, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    search_query = func.plainto_tsquery('english', q)
    metadata_vector = func.to_tsvector(
        'english',
        func.jsonb_path_query_array(ScientificMetadataBinary.scientific_metadata, text("'$[*]'")).cast(Text)
    )
    rank_expr = func.ts_rank_cd(metadata_vector, search_query)

    acl_subquery = exists().where(
        ResourceACL.mfid == ScientificMetadataBinary.unique_id,
        ResourceACL.accessgroup_id.in_(ag_ids)
    )

    stmt = (
        select(ScientificMetadataBinary, rank_expr.label("rank"))
        .join(Resource, Resource.mfid == ScientificMetadataBinary.unique_id)
        .where(
            and_(
                or_(Resource.public == True, acl_subquery),
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


@router.post("/resources/{resource_id}/delete", response_model=DeletionRequestRead)
def create_deletion_request(
    auth_res: Annotated[tuple, Depends(authorize_user)],
    resource_id: str,
    reason: Optional[str] = None,
):
    '''
    Request deletion of a resource. Any authenticated user with access to the resource may submit.
    Returns 409 if an active deletion request already exists for this resource.
    '''
    requester_id, _ = auth_res

    with Session(engine) as session:
        resource = session.exec(
            select(Resource).where(Resource.mfid == resource_id)
        ).one_or_none()
        if resource is None:
            raise HTTPException(status_code=404, detail=f"Resource {resource_id} not found.")

        existing = session.exec(
            select(DeletionRequest)
            .where(DeletionRequest.resource_id == resource_id)
            .where(DeletionRequest.status.in_(["pending", "approved"]))
        ).one_or_none()
        if existing:
            raise HTTPException(
                status_code=409,
                detail=f"A deletion request for {resource_id} already exists (status: {existing.status}).",
            )

        resource_name = None
        if resource.resource_type == "dataset":
            ds = session.exec(select(Dataset).where(Dataset.unique_id == resource_id)).one_or_none()
            resource_name = ds.dataset_name if ds else None
        elif resource.resource_type == "sample":
            samp = session.exec(select(Sample).where(Sample.unique_id == resource_id)).one_or_none()
            resource_name = samp.sample_name if samp else None

        deletion_req = DeletionRequest(
            resource_id=resource_id,
            resource_name=resource_name,
            requester_id=requester_id,
            reason=reason,
            status="pending",
        )
        session.add(deletion_req)
        session.commit()
        session.refresh(deletion_req)
        return DeletionRequestRead(
            **deletion_req.model_dump(),
            resource_type=deletion_req.resource.resource_type if deletion_req.resource else None,
        )

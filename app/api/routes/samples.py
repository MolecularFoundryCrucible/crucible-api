from typing import Annotated, List, Optional, Union

import networkx as nx
from networkx.readwrite import json_graph
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import exists, join, text
from sqlalchemy.orm import joinedload, selectinload
from sqlmodel import and_, or_, Session, select

from app.api.dependencies import get_engine
from app.general_utils import get_tz_isoformat, is_orcid
from app.models.core_models import (
    AccessGroup, Dataset, DatasetACL, DatasetSampleLink,
    DeletionRequest, Resource, Sample, SampleLink, User,
)
from app.models.datasets import DatasetRead
from app.models.samples import SampleCreate, SampleRead, SampleResponse, SampleUpdate
from app.utils.auth_utils import authorize_user, require_groups
from app.utils.sql_utils import add_or_update, generate_kw_query

router = APIRouter(tags=["samples"])
engine = get_engine()


@router.post("/samples/{parent_sample_id}/children/{child_sample_id}", response_model=SampleLink)
def link_parent_child_samples(parent_sample_id, child_sample_id, auth_res: Annotated[tuple, Depends(authorize_user)]):

    with Session(engine) as session:
        parent_sample = session.exec(select(Sample).where(Sample.unique_id == parent_sample_id)).first()
        child_sample = session.exec(select(Sample).where(Sample.unique_id == child_sample_id)).first()
        link = add_or_update(session,
                     SampleLink,
                     where = (and_(SampleLink.parent_sample_id == parent_sample.id, SampleLink.sample_id == child_sample.id)),
                     parent_sample_id = parent_sample.id,
                     sample_id = child_sample.id)

        session.commit()
        session.refresh(link)

        return(link)


@router.delete("/samples/{parent_sample_id}/children/{child_sample_id}")
def unlink_parent_child_samples(parent_sample_id: str, child_sample_id: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    _, ag_list = auth_res
    require_groups(ag_list)
    with Session(engine) as session:
        parent = session.exec(select(Sample).where(Sample.unique_id == parent_sample_id)).one_or_none()
        if not parent:
            raise HTTPException(status_code=404, detail="Parent sample not found")

        child = session.exec(select(Sample).where(Sample.unique_id == child_sample_id)).one_or_none()
        if not child:
            raise HTTPException(status_code=404, detail="Child sample not found")

        link = session.exec(
            select(SampleLink).where(
                and_(SampleLink.parent_sample_id == parent.id,
                     SampleLink.sample_id == child.id)
            )
        ).one_or_none()

        if link is None:
            raise HTTPException(status_code=404, detail="No link found between these samples")

        session.delete(link)
        session.commit()
        return {"message": f"Successfully unlinked {parent_sample_id} -> {child_sample_id}"}


@router.patch('/samples/{sample_id}', response_model=SampleRead)
def update_sample(auth_res: Annotated[tuple, Depends(authorize_user)], sample_id: str, updated_sample: SampleUpdate):
    '''
    Partially update a sample record. Only provided fields are modified.
    '''
    with Session(engine) as session:
        sample = session.exec(select(Sample).where(Sample.unique_id == sample_id)).one_or_none()
        if not sample:
            raise HTTPException(status_code=404, detail='Sample not found')

        for key, value in updated_sample.model_dump(exclude_unset=True).items():
            setattr(sample, key, value)

        ri = session.exec(select(Resource).where(Resource.mfid == sample_id)).one_or_none()
        if ri:
            ri.modification_time = get_tz_isoformat()

        session.add(sample)
        session.commit()
        session.refresh(sample)
        return sample


@router.post("/samples", response_model=SampleRead)
def create_sample(auth_res: Annotated[tuple, Depends(authorize_user)], S: SampleCreate):
    '''
    Create a new sample. If a sample with the same project_id and sample_name already
    exists, the existing record is returned without modification.
    '''
    user_unique_id, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    if not S.sample_name.strip():
        raise HTTPException(status_code=400, detail="sample_name must not be empty.")

    with Session(engine) as session:
        # Dedup: return existing sample if (project_id, sample_name) already registered
        existing = session.exec(
            select(Sample).where(
                and_(Sample.project_id == S.project_id, Sample.sample_name == S.sample_name)
            )
        ).one_or_none()
        if existing:
            return existing

        sample = Sample(**S.model_dump(exclude_unset=True))
        session.add(sample)
        session.commit()
        session.refresh(sample)

        # Resolve owner from auth if not explicitly provided
        if sample.owner_orcid is None and is_orcid(user_unique_id):
            sample.owner_orcid = user_unique_id
        if sample.owner_user_id is None and sample.owner_orcid:
            sample.owner_user_id = session.exec(
                select(User.id).where(User.unique_id == sample.owner_orcid)
            ).one_or_none()

        now = get_tz_isoformat()
        ri = add_or_update(session,
                           Resource,
                           where=(Resource.mfid == sample.unique_id),
                           mfid=sample.unique_id,
                           resource_type="sample")
        ri.creation_time = now
        ri.modification_time = now

        session.add(sample)
        session.commit()
        session.refresh(sample)
        return sample


@router.get("/samples", response_model=List[SampleResponse], response_model_exclude_none=True)
def list_samples(auth_res: Annotated[tuple, Depends(authorize_user)], request: Request,
                 creation_time_gte: Optional[str] = None, creation_time_lte: Optional[str] = None,
                 modification_time_gte: Optional[str] = None, modification_time_lte: Optional[str] = None):
    '''
    Returns a list of samples where the owner_orcid matches the orcid of the person who made the request.
    If an admin API key is used, returns all samples.
    '''
    user_unique_id, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    query_params = request.query_params
    print(query_params)

    params_dict = dict(query_params)
    # Remove explicit query params so they don't get applied as Sample attribute filters
    for k in ('creation_time_gte', 'creation_time_lte', 'modification_time_gte', 'modification_time_lte'):
        params_dict.pop(k, None)
    print(params_dict)

    with Session(engine) as session:

        samp_cmd = select(Sample).join(Resource, Resource.mfid == Sample.unique_id, isouter=True)

        # Exclude soft-deleted resources (pending or approved deletion)
        samp_cmd = samp_cmd.where(
            ~exists().where(
                DeletionRequest.resource_id == Sample.unique_id,
                DeletionRequest.status.in_(["pending", "approved"])
            )
        )

        # Users with admin privileges can see all samples, otherwise filter on owner ID + project. **TODO: need sample ACL like datasets
        associated_group_names = [ag.group_name for ag in ag_list]
        if 'admin' not in associated_group_names:
            print(f'filtering on user_unique_id={user_unique_id}')
            samp_cmd = samp_cmd.where(or_(Sample.owner_orcid == user_unique_id,
                                          Sample.project_id.in_(associated_group_names)))


        # Filter on Resource timestamps
        if creation_time_gte:
            samp_cmd = samp_cmd.where(Resource.creation_time >= creation_time_gte)
        if creation_time_lte:
            samp_cmd = samp_cmd.where(Resource.creation_time <= creation_time_lte)
        if modification_time_gte:
            samp_cmd = samp_cmd.where(Resource.modification_time >= modification_time_gte)
        if modification_time_lte:
            samp_cmd = samp_cmd.where(Resource.modification_time <= modification_time_lte)

        for k, v in params_dict.items():
            if hasattr(Sample, k) and v is not None:
                print(f'filtering results on {k}={v}')
                samp_cmd = samp_cmd.where(getattr(Sample, k) == v)
            else:
               pass

        samp_cmd = samp_cmd.options(selectinload(Sample.datasets), selectinload(Sample.resource))
        samp_list = session.exec(samp_cmd).all()
        return [
            SampleResponse.model_validate({
                **samp.model_dump(),
                "datasets": [DatasetRead.model_validate(ds) for ds in samp.datasets],
                "creation_time": samp.resource.creation_time if samp.resource else None,
                "modification_time": samp.resource.modification_time if samp.resource else None,
            })
            for samp in samp_list
        ]


@router.get("/samples/{sample_id}", response_model=SampleResponse, response_model_exclude_none=True)
def read_sample(sample_id: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    '''
    Returns the sample for the provided ID
    '''
    with Session(engine) as session:
        query = select(Sample).options(selectinload(Sample.datasets), selectinload(Sample.resource)).where(Sample.unique_id == sample_id)
        samp = session.exec(query).first()
        if samp is None:
            return None
        return SampleResponse.model_validate({
            **samp.model_dump(),
            "datasets": [DatasetRead.model_validate(ds) for ds in samp.datasets],
            "creation_time": samp.resource.creation_time if samp.resource else None,
            "modification_time": samp.resource.modification_time if samp.resource else None,
        })


@router.get("/samples/{sample_id}/datasets")
def list_sample_datasets(sample_id: str, auth_res: Annotated[tuple, Depends(authorize_user)], request: Request, include_metadata: bool = False, limit: Optional[int] = None):
    '''
    Returns a list of all datasets linked to a given sample
    '''
    user_unique_id, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    params_dict = dict(request.query_params)
    keyword_filter = params_dict.pop("keyword", None)

    with Session(engine) as session:
        # Verify sample exists
        sample = session.exec(select(Sample).where(Sample.unique_id == sample_id)).one_or_none()

        if sample is None:
            raise HTTPException(status_code=404, detail="Sample not found")

        auth_filter = (DatasetACL.accessgroup_id.in_(ag_ids)) | (Dataset.public == True)
        print(f'{include_metadata=}')
        if include_metadata:
            query = (
                    select(Dataset)
                    .options(selectinload(Dataset.scientific_metadata))
                    .join(DatasetACL, DatasetACL.dataset_id == Dataset.id, isouter=True)
                    .filter(auth_filter)
                )

        else:
            query = (select(Dataset)
                        .select_from(
                            join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id, isouter=True)
                        )
                        .filter(auth_filter)
                    )

        if keyword_filter:
            query = generate_kw_query(session, query, keyword_filter)

        for key, value in params_dict.items():
            if hasattr(Dataset, key):
                query = query.where(getattr(Dataset, key) == value)

        full_query = query.join(DatasetSampleLink,
                                DatasetSampleLink.dataset_id == Dataset.id
                                ).where(
                                    DatasetSampleLink.sample_id == select(Sample.id).where(Sample.unique_id == sample_id)
                                ).where(
                                    ~exists().where(
                                        DeletionRequest.resource_id == Dataset.unique_id,
                                        DeletionRequest.status.in_(["pending", "approved"])
                                    )
                                ).distinct(Dataset.id)

        if limit is not None:
            full_query = full_query.limit(limit)

        datasets = session.exec(full_query).all()
        if include_metadata:
            return [{**dataset.model_dump(),
                    "scientific_metadata": dataset.scientific_metadata.model_dump() if dataset.scientific_metadata else None
                    }
                    for dataset in datasets]
        else:
            return datasets


@router.get("/samples/{child_sample_id}/parents", response_model=List[SampleRead])
def list_parent_samples(child_sample_id: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    '''
    Returns a list of parent samples for a given child sample
    '''
    user_unique_id, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]
    with Session(engine) as session:
        child_sample = session.exec(select(Sample).where(Sample.unique_id == child_sample_id)).one_or_none()

        if child_sample is None:
            raise HTTPException(status_code=404, detail="Child sample not found")

        parent_samples = session.exec(
            select(Sample)
            .select_from(join(Sample, SampleLink, SampleLink.parent_sample_id == Sample.id))
            .where(SampleLink.sample_id == child_sample.id)
            .where(
                ~exists().where(
                    DeletionRequest.resource_id == Sample.unique_id,
                    DeletionRequest.status.in_(["pending", "approved"])
                )
            )
        ).all()
        return parent_samples


@router.get("/samples/{parent_sample_id}/children", response_model=List[SampleRead])
def list_child_samples(parent_sample_id: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    '''
    Returns a list of child samples for a given parent sample
    '''
    user_unique_id, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]
    with Session(engine) as session:
        parent_sample = session.exec(select(Sample).where(Sample.unique_id == parent_sample_id)).one_or_none()

        if parent_sample is None:
            raise HTTPException(status_code=404, detail="Parent sample not found")

        child_samples = session.exec(
            select(Sample)
            .select_from(join(Sample, SampleLink, SampleLink.sample_id == Sample.id))
            .where(SampleLink.parent_sample_id == parent_sample.id)
            .where(
                ~exists().where(
                    DeletionRequest.resource_id == Sample.unique_id,
                    DeletionRequest.status.in_(["pending", "approved"])
                )
            )
        ).all()
        return child_samples

@router.get("/samples/{sample_id}/sample_graph")
def sample_graph(sample_id: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    '''
    Returns a NetworkX node-link graph of all samples connected (via parent/child
    relationships) to the given sample. Loads all samples in the project and
    returns the directed lineage subgraph (ancestors + descendants) of the target.
    '''
    _, ag_list = auth_res

    with Session(engine) as session:
        target = session.exec(
            select(Sample).where(Sample.unique_id == sample_id)
        ).one_or_none()

        if target is None:
            raise HTTPException(status_code=404, detail="Sample not found")

        allowed_groups = {'admin', target.owner_orcid, target.project_id}
        require_groups(ag_list, allowed_groups)

        samp_list = session.exec(
            select(Sample)
            .options(joinedload(Sample.parents))
            .where(Sample.project_id == target.project_id)
        ).unique().all()

        edges = []
        for sample in samp_list:
            for parent in sample.parents:
                edges.append((parent.unique_id, sample.unique_id))

        G = nx.DiGraph(edges)
        for sample in samp_list:
            G.add_node(sample.unique_id, sample_name=sample.sample_name)

        if target.unique_id not in G:
            G.add_node(target.unique_id, sample_name=target.sample_name)

        # Use directed ancestors + descendants rather than the weakly connected
        # component. WCC would include siblings (nodes that share a parent with
        # the target but are not in its direct lineage). nx.ancestors/descendants
        # traverses strictly along the target's own lineage chain in both
        # directions, excluding siblings.
        component_nodes = nx.ancestors(G, target.unique_id) | nx.descendants(G, target.unique_id) | {target.unique_id}
        return json_graph.node_link_data(G.subgraph(component_nodes))


@router.get("/samples/{sample_id}/sample_graph_cte")
def sample_graph_cte(sample_id: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    '''
    CTE-based implementation (kept for reference). Prefer /sample_graph.
    Returns a NetworkX node-link graph of all samples connected (via parent/child
    relationships) to the given sample, i.e. its full lineage subgraph.
    '''
    _, ag_list = auth_res
    
    with Session(engine) as session:
        target = session.exec(
            select(Sample).where(Sample.unique_id == sample_id)
        ).one_or_none()

        if target is None:
            raise HTTPException(status_code=404, detail="Sample not found")

        # Auth check: mirrors project_sample_graph logic
        allowed_groups = {'admin', target.owner_orcid, target.project_id}
        require_groups(ag_list, allowed_groups)

        # Two recursive CTEs — one walking up (ancestors), one walking down
        # (descendants). PostgreSQL only allows a single UNION between the
        # non-recursive and recursive terms, so bidirectional traversal in a
        # single CTE is not permitted.
        cte_sql = text("""
            WITH RECURSIVE
            ancestors(id, unique_id, sample_name) AS (
                SELECT id, unique_id, sample_name
                FROM sample WHERE unique_id = :sample_uid
              UNION
                SELECT s.id, s.unique_id, s.sample_name
                FROM sample s
                JOIN samplelink sl ON sl.parent_sample_id = s.id
                JOIN ancestors a ON sl.sample_id = a.id
            ),
            descendants(id, unique_id, sample_name) AS (
                SELECT id, unique_id, sample_name
                FROM sample WHERE unique_id = :sample_uid
              UNION
                SELECT s.id, s.unique_id, s.sample_name
                FROM sample s
                JOIN samplelink sl ON sl.sample_id = s.id
                JOIN descendants d ON sl.parent_sample_id = d.id
            )
            SELECT DISTINCT id, unique_id, sample_name
            FROM (SELECT * FROM ancestors UNION ALL SELECT * FROM descendants) combined
        """)
        rows = session.execute(cte_sql, {"sample_uid": sample_id}).all()

        id_to_uid = {r.id: r.unique_id for r in rows}
        node_ids = list(id_to_uid.keys())

        # Fetch all edges whose both endpoints are within the component
        edge_rows = session.exec(
            select(SampleLink.parent_sample_id, SampleLink.sample_id)
            .where(SampleLink.parent_sample_id.in_(node_ids))
            .where(SampleLink.sample_id.in_(node_ids))
        ).all()

        G = nx.DiGraph((id_to_uid[r[0]], id_to_uid[r[1]]) for r in edge_rows)
        for r in rows:
            G.add_node(r.unique_id, sample_name=r.sample_name)

        return json_graph.node_link_data(G)
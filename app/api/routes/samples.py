from typing import Annotated, List, Optional, Union

import networkx as nx
from networkx.readwrite import json_graph
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import join, text
from sqlalchemy.orm import joinedload, selectinload
from sqlmodel import and_, or_, Session, select

from app.api.dependencies import get_engine
from app.general_utils import get_tz_isoformat, is_orcid
from app.models.core_models import (
    AccessGroup, Dataset, DatasetACL, DatasetSampleLink,
    Sample, SampleLink, User,
)
from app.models.misc import Resource
from app.models.response_schemas import SampleWithDatasetsResponse
from app.utils.auth_utils import authorize_user
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


@router.patch('/samples/{sample_id}', response_model=Sample)
def update_sample(auth_res: Annotated[tuple, Depends(authorize_user)], sample_id: str, updated_sample: Sample):
    access_group_name, ag_ids = auth_res
    cmd = select(Sample).where(Sample.unique_id == sample_id)
    update_fields = updated_sample.model_dump(exclude_unset=True)
    with Session(engine) as session:
        sample = session.exec(cmd).one_or_none()
        if not sample:
            raise HTTPException(status_code=404, detail='Sample not found')

        for key, value in update_fields.items():
            setattr(sample, key, value)
            print(key, value)

        ri = session.exec(select(Resource).where(Resource.mfid == sample_id)).one_or_none()
        if ri:
            ri.modification_time = get_tz_isoformat()

        session.add(sample)
        session.commit()
        session.refresh(sample)
        return sample


@router.post("/samples", response_model=Sample)
def create_sample(auth_res: Annotated[tuple, Depends(authorize_user)], S: Sample):
    """
    Create a new sample in the database.

    Args:
        auth_res: Authorization result containing access group info
        S: Sample object to be added

    Returns:
        Added sample object

    Raises:
        HTTPException: If required sample fields are missing
    """
    access_group_name, ag_ids = auth_res
    
    # Validate required fields
    missing_fields = []
    if not S.sample_name or not S.sample_name.strip():
        missing_fields.append("sample_name")

    if missing_fields:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required sample information: {', '.join(missing_fields)}. \
                     Please provide all required fields: sample_name."
        )

    with Session(engine) as session:
        print("starting sample addition")
        where = or_(Sample.unique_id == S.unique_id,
                and_(Sample.project_id == S.project_id,
                     Sample.sample_name == S.sample_name))
        statement = select(Sample).where(where)
        sample = session.exec(statement).one_or_none()

        print(f'{sample=}')
        if sample:
            return sample

        sample = Sample(**S.model_dump(exclude_unset=True, exclude={"parents", "children"}))
        print(f'{sample=}')
        session.add(sample)
        session.commit()
        session.refresh(sample)

        if sample.owner_user_id is None and sample.owner_orcid is None:
            creator = session.exec(select(AccessGroup).where(AccessGroup.group_name == access_group_name)).one_or_none()
            if is_orcid(access_group_name):
                sample.owner_orcid = access_group_name
                creator_user_id = session.exec(select(User.id).where(User.orcid == sample.owner_orcid)).one_or_none()
                sample.owner_user_id = creator_user_id

        elif sample.owner_user_id is None:
            creator_user_id = session.exec(select(User.id).where(User.orcid == sample.owner_orcid)).one_or_none()
            sample.owner_user_id = creator_user_id

        else:
            pass

        session.add(sample)
        now = get_tz_isoformat()
        ri = add_or_update(session,
                            Resource,
                            where=(Resource.mfid == sample.unique_id),
                            mfid=sample.unique_id,
                            resource_type="sample")
        ri.creation_time = now
        ri.modification_time = now

        session.commit()
        print('final commit')
        session.refresh(sample)
        return(sample)


@router.get("/samples")
def list_samples(auth_res: Annotated[tuple, Depends(authorize_user)], request: Request,
                 creation_time_gte: Optional[str] = None, creation_time_lte: Optional[str] = None,
                 modification_time_gte: Optional[str] = None, modification_time_lte: Optional[str] = None):
    '''
    Returns a list of samples where the owner_orcid matches the orcid of the person who made the request.
    If an admin API key is used, returns all samples.
    '''
    access_group_name, ag_ids = auth_res

    query_params = request.query_params
    print(query_params)

    params_dict = dict(query_params)
    # Remove explicit query params so they don't get applied as Sample attribute filters
    for k in ('creation_time_gte', 'creation_time_lte', 'modification_time_gte', 'modification_time_lte'):
        params_dict.pop(k, None)
    print(params_dict)

    with Session(engine) as session:

        samp_cmd = select(Sample).join(Resource, Resource.mfid == Sample.unique_id, isouter=True)

        # Admin API keys can see all samples
        if access_group_name.startswith('admin'):
            print('using admin account')
            pass
        elif is_orcid(access_group_name):
            print(f'filtering on owner_orcid={access_group_name}')
            associated_groups = session.exec(select(AccessGroup).where(AccessGroup.id.in_(ag_ids))).all()
            associated_group_names = [ag.group_name for ag in associated_groups]
            samp_cmd = samp_cmd.where(or_(Sample.owner_orcid == access_group_name,
                                          Sample.project_id.in_(associated_group_names)))
        else:
            print(f'apikey does not have access to samples')
            return []

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
        return [{**samp.model_dump(),
                "datasets": samp.datasets,
                "creation_time": samp.resource.creation_time if samp.resource else None,
                "modification_time": samp.resource.modification_time if samp.resource else None}
                for samp in samp_list]


@router.get("/samples/{sample_id}")
def read_sample(sample_id: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    '''
    Returns the sample for the provided ID
    '''
    with Session(engine) as session:
        query = select(Sample).options(selectinload(Sample.datasets), selectinload(Sample.resource)).where(Sample.unique_id == sample_id)
        samp = session.exec(query).first()
        if samp is None:
            return None
        return {**samp.model_dump(),
                "datasets": samp.datasets,
                "creation_time": samp.resource.creation_time if samp.resource else None,
                "modification_time": samp.resource.modification_time if samp.resource else None}


@router.get("/samples/{sample_id}/datasets")
def list_sample_datasets(sample_id: str, auth_res: Annotated[tuple, Depends(authorize_user)], request: Request, include_metadata: bool = False, limit: Optional[int] = None):
    '''
    Returns a list of all datasets linked to a given sample
    '''
    access_group_name, ag_ids = auth_res

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


@router.get("/samples/{child_sample_id}/parents", response_model=List[Sample])
def list_parent_samples(child_sample_id: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    '''
    Returns a list of parent samples for a given child sample
    '''
    access_group_name, ag_ids = auth_res
    with Session(engine) as session:
        child_sample = session.exec(select(Sample).where(Sample.unique_id == child_sample_id)).one_or_none()

        if child_sample is None:
            raise HTTPException(status_code=404, detail="Child sample not found")

        parent_samples = session.exec(
            select(Sample)
            .select_from(join(Sample, SampleLink, SampleLink.parent_sample_id == Sample.id))
            .where(SampleLink.sample_id == child_sample.id)
        ).all()
        return parent_samples


@router.get("/samples/{parent_sample_id}/children", response_model=List[Sample])
def list_child_samples(parent_sample_id: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    '''
    Returns a list of child samples for a given parent sample
    '''
    access_group_name, ag_ids = auth_res
    with Session(engine) as session:
        parent_sample = session.exec(select(Sample).where(Sample.unique_id == parent_sample_id)).one_or_none()

        if parent_sample is None:
            raise HTTPException(status_code=404, detail="Parent sample not found")

        child_samples = session.exec(
            select(Sample)
            .select_from(join(Sample, SampleLink, SampleLink.sample_id == Sample.id))
            .where(SampleLink.parent_sample_id == parent_sample.id)
        ).all()
        return child_samples

@router.get("/samples/{sample_id}/sample_graph")
def sample_graph(sample_id: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    '''
    Returns a NetworkX node-link graph of all samples connected (via parent/child
    relationships) to the given sample. Loads all samples in the project and
    returns the directed lineage subgraph (ancestors + descendants) of the target.
    '''
    access_group_name, ag_ids = auth_res

    with Session(engine) as session:
        target = session.exec(
            select(Sample).where(Sample.unique_id == sample_id)
        ).one_or_none()

        if target is None:
            raise HTTPException(status_code=404, detail="Sample not found")

        if access_group_name.startswith('admin'):
            pass
        elif is_orcid(access_group_name):
            associated_groups = session.exec(select(AccessGroup).where(AccessGroup.id.in_(ag_ids))).all()
            associated_group_names = [ag.group_name for ag in associated_groups]
            if target.owner_orcid != access_group_name and target.project_id not in associated_group_names:
                raise HTTPException(status_code=403, detail="Access denied")
        else:
            raise HTTPException(status_code=403, detail="Access denied")

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
    access_group_name, ag_ids = auth_res

    with Session(engine) as session:
        target = session.exec(
            select(Sample).where(Sample.unique_id == sample_id)
        ).one_or_none()

        if target is None:
            raise HTTPException(status_code=404, detail="Sample not found")

        # Auth check: mirrors project_sample_graph logic
        if access_group_name.startswith('admin'):
            pass
        elif is_orcid(access_group_name):
            associated_groups = session.exec(select(AccessGroup).where(AccessGroup.id.in_(ag_ids))).all()
            associated_group_names = [ag.group_name for ag in associated_groups]
            if target.owner_orcid != access_group_name and target.project_id not in associated_group_names:
                raise HTTPException(status_code=403, detail="Access denied")
        else:
            raise HTTPException(status_code=403, detail="Access denied")

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
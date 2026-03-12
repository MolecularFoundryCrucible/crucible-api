from typing import Annotated

import networkx as nx
from networkx.readwrite import json_graph
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import joinedload
from sqlmodel import or_, Session, select

from app.api.dependencies import get_engine
from app.general_utils import is_orcid
from app.models.core_models import (
    AccessGroup, Dataset, DatasetACL, DatasetLink, DatasetSampleLink,
    Sample, SampleLink,
)
from app.utils.auth_utils import authorize_user

router = APIRouter(tags=["entities"])
engine = get_engine()


@router.get("/entity_graph/{entity_id}")
def entity_graph(entity_id: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    '''
    Returns a NetworkX node-link graph of all samples and datasets connected to
    the given entity via sample lineage, dataset lineage, and sample-dataset
    associations. entity_id may be a sample or dataset unique_id.
    Nodes carry entity_type ('sample' or 'dataset') and name attributes.

    Loads all samples and datasets in the root entity's project in a small number
    of broad queries, builds the combined graph in Python, then returns the weakly
    connected component containing the root entity.
    '''
    access_group_name, ag_ids = auth_res

    with Session(engine) as session:
        # Identify root entity type, enforce auth, and determine project scope
        sample_target = session.exec(
            select(Sample).where(Sample.unique_id == entity_id)
        ).one_or_none()

        if sample_target is not None:
            if access_group_name.startswith('admin'):
                pass
            elif is_orcid(access_group_name):
                associated_groups = session.exec(select(AccessGroup).where(AccessGroup.id.in_(ag_ids))).all()
                associated_group_names = [ag.group_name for ag in associated_groups]
                if sample_target.owner_orcid != access_group_name and sample_target.project_id not in associated_group_names:
                    raise HTTPException(status_code=403, detail="Access denied")
            else:
                raise HTTPException(status_code=403, detail="Access denied")
            project_id = sample_target.project_id
        else:
            auth_filter = (DatasetACL.accessgroup_id.in_(ag_ids)) | (Dataset.public == True)
            dataset_target = session.exec(
                select(Dataset)
                .join(DatasetACL, DatasetACL.dataset_id == Dataset.id, isouter=True)
                .filter(auth_filter)
                .where(Dataset.unique_id == entity_id)
            ).first()
            if dataset_target is None:
                raise HTTPException(status_code=404, detail="Entity not found")
            project_id = dataset_target.project_id

        # Load all samples in the project with parent relationships
        samp_list = session.exec(
            select(Sample)
            .options(joinedload(Sample.parents))
            .where(Sample.project_id == project_id)
        ).unique().all()

        # Load all authorized datasets in the project with parent relationships
        auth_filter = (DatasetACL.accessgroup_id.in_(ag_ids)) | (Dataset.public == True)
        ds_list = session.exec(
            select(Dataset)
            .options(joinedload(Dataset.parents))
            .join(DatasetACL, DatasetACL.dataset_id == Dataset.id, isouter=True)
            .filter(auth_filter)
            .where(Dataset.project_id == project_id)
        ).unique().all()

        # Map integer FK → unique_id for resolving DatasetSampleLink rows
        s_int_to_uid = {s.id: s.unique_id for s in samp_list}
        d_int_to_uid = {d.id: d.unique_id for d in ds_list}

        # Load sample-dataset associations scoped to this project's entities
        sd_links = session.exec(
            select(DatasetSampleLink)
            .where(DatasetSampleLink.sample_id.in_(s_int_to_uid.keys()))
            .where(DatasetSampleLink.dataset_id.in_(d_int_to_uid.keys()))
        ).all() if s_int_to_uid and d_int_to_uid else []

        # Build unique_id digraphs for each type separately so directed lineage
        # traversal stays within one entity type at a time.
        G_samp = nx.DiGraph()
        for s in samp_list:
            G_samp.add_node(s.unique_id)
            for parent in s.parents:
                G_samp.add_edge(parent.unique_id, s.unique_id)

        G_ds = nx.DiGraph()
        for d in ds_list:
            G_ds.add_node(d.unique_id)
            for parent in d.parents:
                G_ds.add_edge(parent.unique_id, d.unique_id)

        # Build sample↔dataset adjacency keyed by unique_id
        s_to_ds: dict[str, set[str]] = {}
        ds_to_s: dict[str, set[str]] = {}
        for link in sd_links:
            s_uid = s_int_to_uid[link.sample_id]
            d_uid = d_int_to_uid[link.dataset_id]
            s_to_ds.setdefault(s_uid, set()).add(d_uid)
            ds_to_s.setdefault(d_uid, set()).add(s_uid)

        def lineage(G: nx.DiGraph, seeds: set) -> set:
            """Bidirectional directed lineage from a set of seed unique_ids."""
            result = set()
            for n in seeds:
                if n in G:
                    result |= nx.ancestors(G, n) | nx.descendants(G, n)
                result.add(n)
            return result

        # Two-step expansion mirroring the BFS version:
        #   sample root → full sample lineage → linked datasets → dataset lineage
        #   dataset root → full dataset lineage → linked samples → sample lineage
        if sample_target is not None:
            sample_uids = lineage(G_samp, {sample_target.unique_id})
            seed_ds = set().union(*(s_to_ds.get(s, set()) for s in sample_uids))
            ds_uids = lineage(G_ds, seed_ds)
        else:
            ds_uids = lineage(G_ds, {dataset_target.unique_id})
            seed_s = set().union(*(ds_to_s.get(d, set()) for d in ds_uids))
            sample_uids = lineage(G_samp, seed_s)

        # Build output graph
        G = nx.DiGraph()
        for s in samp_list:
            if s.unique_id in sample_uids:
                G.add_node(s.unique_id, entity_type='sample', name=s.sample_name)
                for parent in s.parents:
                    if parent.unique_id in sample_uids:
                        G.add_edge(parent.unique_id, s.unique_id)
        for d in ds_list:
            if d.unique_id in ds_uids:
                G.add_node(d.unique_id, entity_type='dataset', name=d.dataset_name)
                for parent in d.parents:
                    if parent.unique_id in ds_uids:
                        G.add_edge(parent.unique_id, d.unique_id)
        for link in sd_links:
            s_uid = s_int_to_uid[link.sample_id]
            d_uid = d_int_to_uid[link.dataset_id]
            if s_uid in sample_uids and d_uid in ds_uids:
                G.add_edge(s_uid, d_uid)

        return json_graph.node_link_data(G)


@router.get("/entity_graph_bfs/{entity_id}")
def entity_graph_bfs(entity_id: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    '''
    BFS-based implementation (kept for reference). Prefer /entity_graph/{entity_id}.
    Returns a NetworkX node-link graph of all samples and datasets connected to
    the given entity via sample lineage, dataset lineage, and sample-dataset
    associations. entity_id may be a sample or dataset unique_id.
    Nodes carry entity_type ('sample' or 'dataset') and name attributes.
    '''
    access_group_name, ag_ids = auth_res

    with Session(engine) as session:
        # Identify root entity type and enforce auth before traversal
        sample_target = session.exec(
            select(Sample).where(Sample.unique_id == entity_id)
        ).one_or_none()

        if sample_target is not None:
            if access_group_name.startswith('admin'):
                pass
            elif is_orcid(access_group_name):
                associated_groups = session.exec(select(AccessGroup).where(AccessGroup.id.in_(ag_ids))).all()
                associated_group_names = [ag.group_name for ag in associated_groups]
                if sample_target.owner_orcid != access_group_name and sample_target.project_id not in associated_group_names:
                    raise HTTPException(status_code=403, detail="Access denied")
            else:
                raise HTTPException(status_code=403, detail="Access denied")
        else:
            auth_filter = (DatasetACL.accessgroup_id.in_(ag_ids)) | (Dataset.public == True)
            dataset_target = session.exec(
                select(Dataset)
                .join(DatasetACL, DatasetACL.dataset_id == Dataset.id, isouter=True)
                .filter(auth_filter)
                .where(Dataset.unique_id == entity_id)
            ).first()
            if dataset_target is None:
                raise HTTPException(status_code=404, detail="Entity not found")

        # Directional traversal — walks strictly up (ancestors) and strictly
        # down (descendants) for each entity type separately, preventing
        # siblings from being included via a shared parent node.
        #
        # Cross-type links flow one way: samples → datasets.
        # Datasets are never used to discover additional samples.

        def walk_up_samples(start: set) -> set:
            ids = set(start)
            frontier = set(start)
            while frontier:
                rows = session.exec(
                    select(Sample.id)
                    .join(SampleLink, SampleLink.parent_sample_id == Sample.id)
                    .where(SampleLink.sample_id.in_(frontier))
                ).all()
                frontier = set(rows) - ids
                ids |= frontier
            return ids

        def walk_down_samples(start: set) -> set:
            ids = set(start)
            frontier = set(start)
            while frontier:
                rows = session.exec(
                    select(Sample.id)
                    .join(SampleLink, SampleLink.sample_id == Sample.id)
                    .where(SampleLink.parent_sample_id.in_(frontier))
                ).all()
                frontier = set(rows) - ids
                ids |= frontier
            return ids

        def walk_up_datasets(start: set) -> set:
            ids = set(start)
            frontier = set(start)
            while frontier:
                rows = session.exec(
                    select(Dataset.id)
                    .join(DatasetLink, DatasetLink.parent_dataset_id == Dataset.id)
                    .where(DatasetLink.child_dataset_id.in_(frontier))
                ).all()
                frontier = set(rows) - ids
                ids |= frontier
            return ids

        def walk_down_datasets(start: set) -> set:
            ids = set(start)
            frontier = set(start)
            while frontier:
                rows = session.exec(
                    select(Dataset.id)
                    .join(DatasetLink, DatasetLink.child_dataset_id == Dataset.id)
                    .where(DatasetLink.parent_dataset_id.in_(frontier))
                ).all()
                frontier = set(rows) - ids
                ids |= frontier
            return ids

        if sample_target is not None:
            root_s = {sample_target.id}
            sample_ids = walk_up_samples(root_s) | walk_down_samples(root_s)

            linked_ds = session.exec(
                select(Dataset.id)
                .join(DatasetSampleLink, DatasetSampleLink.dataset_id == Dataset.id)
                .where(DatasetSampleLink.sample_id.in_(sample_ids))
            ).all()
            seed_ds = set(linked_ds)
        else:
            root_d = {dataset_target.id}
            seed_ds = walk_up_datasets(root_d) | walk_down_datasets(root_d)

            linked_s = session.exec(
                select(Sample.id)
                .join(DatasetSampleLink, DatasetSampleLink.sample_id == Sample.id)
                .where(DatasetSampleLink.dataset_id.in_(seed_ds))
            ).all()
            seed_s = set(linked_s)
            sample_ids = walk_up_samples(seed_s) | walk_down_samples(seed_s)

        dataset_ids = walk_up_datasets(seed_ds) | walk_down_datasets(seed_ds) if seed_ds else set()

        # Fetch node data
        sample_rows = session.exec(
            select(Sample.id, Sample.unique_id, Sample.sample_name)
            .where(Sample.id.in_(sample_ids))
        ).all() if sample_ids else []

        dataset_rows = session.exec(
            select(Dataset.id, Dataset.unique_id, Dataset.dataset_name)
            .where(Dataset.id.in_(dataset_ids))
        ).all() if dataset_ids else []

        id_to_uid: dict = {}
        for r in sample_rows:
            id_to_uid[('sample', r[0])] = (r[1], r[2])
        for r in dataset_rows:
            id_to_uid[('dataset', r[0])] = (r[1], r[2])

        edges = []

        if sample_ids:
            sample_edges = session.exec(
                select(SampleLink.parent_sample_id, SampleLink.sample_id)
                .where(SampleLink.parent_sample_id.in_(sample_ids))
                .where(SampleLink.sample_id.in_(sample_ids))
            ).all()
            edges += [(id_to_uid[('sample', r[0])][0], id_to_uid[('sample', r[1])][0]) for r in sample_edges]

        if dataset_ids:
            dataset_edges = session.exec(
                select(DatasetLink.parent_dataset_id, DatasetLink.child_dataset_id)
                .where(DatasetLink.parent_dataset_id.in_(dataset_ids))
                .where(DatasetLink.child_dataset_id.in_(dataset_ids))
            ).all()
            edges += [(id_to_uid[('dataset', r[0])][0], id_to_uid[('dataset', r[1])][0]) for r in dataset_edges]

        if sample_ids and dataset_ids:
            sd_edges = session.exec(
                select(DatasetSampleLink.sample_id, DatasetSampleLink.dataset_id)
                .where(DatasetSampleLink.sample_id.in_(sample_ids))
                .where(DatasetSampleLink.dataset_id.in_(dataset_ids))
            ).all()
            edges += [(id_to_uid[('sample', r[0])][0], id_to_uid[('dataset', r[1])][0]) for r in sd_edges]

        G = nx.DiGraph(edges)
        for (entity_type, _), (unique_id, name) in id_to_uid.items():
            G.add_node(unique_id, entity_type=entity_type, name=name)

        return json_graph.node_link_data(G)

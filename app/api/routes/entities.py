from typing import Annotated

import networkx as nx
from networkx.readwrite import json_graph
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text, bindparam, ARRAY, Integer
from sqlalchemy.orm import joinedload
from sqlmodel import or_, Session, select

from app.api.dependencies import get_engine
from app.models.core_models import (
    AccessGroup, Dataset, DeletionRequest, Resource, ResourceACL,
    DatasetLink, DatasetSampleLink, Sample, SampleLink,
)
from app.utils.auth_utils import authorize_user, read_access_filter_list
from app.utils.permissions import Principal

router = APIRouter(tags=["entities"])
engine = get_engine()


@router.get("/entity_graph/{entity_id}")
def entity_graph(entity_id: str, auth_res: Annotated[Principal, Depends(authorize_user)]):
    '''
    Returns a NetworkX node-link graph of all samples and datasets connected to
    the given entity via sample lineage, dataset lineage, and sample-dataset
    associations. entity_id may be a sample or dataset unique_id.
    Nodes carry entity_type ('sample' or 'dataset') and name attributes.

    Loads all samples and datasets in the root entity's project in a small number
    of broad queries, builds the combined graph in Python, then returns the weakly
    connected component containing the root entity.
    '''
    user_unique_id, ag_list = auth_res.user_id, auth_res.groups
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        ds_auth, ds_del = read_access_filter_list(Dataset, ag_ids)
        s_auth, s_del = read_access_filter_list(Sample, ag_ids)

        # Identify root entity type, enforce auth, and determine project scope
        sample_target = session.exec(
            select(Sample)
            .join(Resource, Resource.mfid == Sample.unique_id, isouter=True)
            .where(s_auth).where(s_del)
            .where(Sample.unique_id == entity_id)
        ).first()

        if sample_target is not None:
            project_id = sample_target.project_id
        else:
            dataset_target = session.exec(
                select(Dataset)
                .join(Resource, Resource.mfid == Dataset.unique_id, isouter=True)
                .where(ds_auth).where(ds_del)
                .where(Dataset.unique_id == entity_id)
            ).first()
            if dataset_target is None:
                raise HTTPException(status_code=404, detail="Entity not found")
            project_id = dataset_target.project_id

        # Load all authorized samples in the project with parent relationships
        samp_list = session.exec(
            select(Sample)
            .options(joinedload(Sample.parents))
            .join(Resource, Resource.mfid == Sample.unique_id, isouter=True)
            .where(s_auth).where(s_del)
            .where(Sample.project_id == project_id)
        ).unique().all()

        # Load all authorized datasets in the project with parent relationships
        ds_list = session.exec(
            select(Dataset)
            .options(joinedload(Dataset.parents))
            .join(Resource, Resource.mfid == Dataset.unique_id, isouter=True)
            .where(ds_auth).where(ds_del)
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
def entity_graph_bfs(entity_id: str, auth_res: Annotated[Principal, Depends(authorize_user)]):
    '''
    BFS-based implementation (kept for reference). Prefer /entity_graph/{entity_id}.
    Returns a NetworkX node-link graph of all samples and datasets connected to
    the given entity via sample lineage, dataset lineage, and sample-dataset
    associations. entity_id may be a sample or dataset unique_id.
    Nodes carry entity_type ('sample' or 'dataset') and name attributes.
    '''
    user_unique_id, ag_list = auth_res.user_id, auth_res.groups
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        ds_auth, ds_del = read_access_filter_list(Dataset, ag_ids)
        s_auth, s_del = read_access_filter_list(Sample, ag_ids)

        # Identify root entity type and enforce auth before traversal
        sample_target = session.exec(
            select(Sample)
            .join(Resource, Resource.mfid == Sample.unique_id, isouter=True)
            .where(s_auth).where(s_del)
            .where(Sample.unique_id == entity_id)
        ).first()

        if sample_target is None:
            dataset_target = session.exec(
                select(Dataset)
                .join(Resource, Resource.mfid == Dataset.unique_id, isouter=True)
                .where(ds_auth).where(ds_del)
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

        # The walks return raw IDs without ACL/deletion checks. Restrict node
        # fetches to authorized, non-deleted resources and trim the ID sets so
        # edges referencing dropped nodes are excluded below.
        sample_rows = session.exec(
            select(Sample.id, Sample.unique_id, Sample.sample_name)
            .join(Resource, Resource.mfid == Sample.unique_id, isouter=True)
            .where(s_auth).where(s_del)
            .where(Sample.id.in_(sample_ids))
        ).all() if sample_ids else []
        sample_ids = {r[0] for r in sample_rows}

        dataset_rows = session.exec(
            select(Dataset.id, Dataset.unique_id, Dataset.dataset_name)
            .join(Resource, Resource.mfid == Dataset.unique_id, isouter=True)
            .where(ds_auth).where(ds_del)
            .where(Dataset.id.in_(dataset_ids))
        ).all() if dataset_ids else []
        dataset_ids = {r[0] for r in dataset_rows}

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

# ---------------------------------------------------------------------------
# Shared helper: fetch node data + edges for a known set of IDs and build graph
# ---------------------------------------------------------------------------

def _build_graph(session: Session, sample_ids: set, dataset_ids: set) -> dict:
    sample_rows = (
        session.exec(
            select(Sample.id, Sample.unique_id, Sample.sample_name).where(
                Sample.id.in_(sample_ids)
            )
        ).all()
        if sample_ids else []
    )
    dataset_rows = (
        session.exec(
            select(Dataset.id, Dataset.unique_id, Dataset.dataset_name).where(
                Dataset.id.in_(dataset_ids)
            )
        ).all()
        if dataset_ids else []
    )

    # integer DB id → unique_id string (for edge resolution)
    s_id_to_uid: dict = {}
    d_id_to_uid: dict = {}
    nodes = []
    for r in sample_rows:
        s_id_to_uid[r[0]] = r[1]
        nodes.append({"id": r[1], "entity_type": "sample", "name": r[2]})
    for r in dataset_rows:
        d_id_to_uid[r[0]] = r[1]
        nodes.append({"id": r[1], "entity_type": "dataset", "name": r[2]})

    links = []

    if sample_ids:
        sample_edges = session.exec(
            select(SampleLink.parent_sample_id, SampleLink.sample_id)
            .where(SampleLink.parent_sample_id.in_(sample_ids))
            .where(SampleLink.sample_id.in_(sample_ids))
        ).all()
        links += [
            {"source": s_id_to_uid[r[0]], "target": s_id_to_uid[r[1]]}
            for r in sample_edges
        ]

    if dataset_ids:
        dataset_edges = session.exec(
            select(DatasetLink.parent_dataset_id, DatasetLink.child_dataset_id)
            .where(DatasetLink.parent_dataset_id.in_(dataset_ids))
            .where(DatasetLink.child_dataset_id.in_(dataset_ids))
        ).all()
        links += [
            {"source": d_id_to_uid[r[0]], "target": d_id_to_uid[r[1]]}
            for r in dataset_edges
        ]

    if sample_ids and dataset_ids:
        sd_edges = session.exec(
            select(DatasetSampleLink.sample_id, DatasetSampleLink.dataset_id)
            .where(DatasetSampleLink.sample_id.in_(sample_ids))
            .where(DatasetSampleLink.dataset_id.in_(dataset_ids))
        ).all()
        links += [
            {"source": s_id_to_uid[r[0]], "target": d_id_to_uid[r[1]]}
            for r in sd_edges
        ]

    return {
        "directed": True,
        "multigraph": False,
        "graph": {},
        "nodes": nodes,
        "edges": links,
    }


# ---------------------------------------------------------------------------
# GET /entity_graph_cte/{entity_id}
# CTE-based: finds the connected component in a fixed number of queries.
# Default: first-degree neighbours only. Pass recursive=true for full traversal.
# ---------------------------------------------------------------------------

_SAMPLE_SEED_1HOP_SQL = text("""
    WITH
        s_neighbors(id) AS (
            SELECT CAST(:seed_id AS int)
            UNION
            SELECT sl.sample_id        FROM samplelink sl WHERE sl.parent_sample_id = CAST(:seed_id AS int)
            UNION
            SELECT sl.parent_sample_id FROM samplelink sl WHERE sl.sample_id        = CAST(:seed_id AS int)
        ),
        d_neighbors(id) AS (
            SELECT DISTINCT dsl.dataset_id FROM datasetsamplelink dsl
             WHERE dsl.sample_id IN (SELECT id FROM s_neighbors)
        )
    SELECT 'sn' AS t, s.unique_id AS a, s.sample_name AS b
      FROM sample s WHERE s.id IN (SELECT id FROM s_neighbors)
    UNION ALL
    SELECT 'dn', d.unique_id, d.dataset_name
      FROM dataset d WHERE d.id IN (SELECT id FROM d_neighbors)
    UNION ALL
    SELECT 'se', s1.unique_id, s2.unique_id
      FROM samplelink sl
      JOIN sample s1 ON s1.id = sl.parent_sample_id
      JOIN sample s2 ON s2.id = sl.sample_id
     WHERE sl.parent_sample_id IN (SELECT id FROM s_neighbors)
       AND sl.sample_id        IN (SELECT id FROM s_neighbors)
    UNION ALL
    SELECT 'de', d1.unique_id, d2.unique_id
      FROM datasetlink dl
      JOIN dataset d1 ON d1.id = dl.parent_dataset_id
      JOIN dataset d2 ON d2.id = dl.child_dataset_id
     WHERE dl.parent_dataset_id IN (SELECT id FROM d_neighbors)
       AND dl.child_dataset_id  IN (SELECT id FROM d_neighbors)
    UNION ALL
    SELECT 'sd', s.unique_id, d.unique_id
      FROM datasetsamplelink dsl
      JOIN sample  s ON s.id = dsl.sample_id
      JOIN dataset d ON d.id = dsl.dataset_id
     WHERE dsl.sample_id  IN (SELECT id FROM s_neighbors)
       AND dsl.dataset_id IN (SELECT id FROM d_neighbors)
""")

_DATASET_SEED_1HOP_SQL = text("""
    WITH
        d_neighbors(id) AS (
            SELECT CAST(:seed_id AS int)
            UNION
            SELECT dl.child_dataset_id  FROM datasetlink dl WHERE dl.parent_dataset_id = CAST(:seed_id AS int)
            UNION
            SELECT dl.parent_dataset_id FROM datasetlink dl WHERE dl.child_dataset_id  = CAST(:seed_id AS int)
        ),
        s_neighbors(id) AS (
            SELECT DISTINCT dsl.sample_id FROM datasetsamplelink dsl
             WHERE dsl.dataset_id IN (SELECT id FROM d_neighbors)
        )
    SELECT 'sn' AS t, s.unique_id AS a, s.sample_name AS b
      FROM sample s WHERE s.id IN (SELECT id FROM s_neighbors)
    UNION ALL
    SELECT 'dn', d.unique_id, d.dataset_name
      FROM dataset d WHERE d.id IN (SELECT id FROM d_neighbors)
    UNION ALL
    SELECT 'se', s1.unique_id, s2.unique_id
      FROM samplelink sl
      JOIN sample s1 ON s1.id = sl.parent_sample_id
      JOIN sample s2 ON s2.id = sl.sample_id
     WHERE sl.parent_sample_id IN (SELECT id FROM s_neighbors)
       AND sl.sample_id        IN (SELECT id FROM s_neighbors)
    UNION ALL
    SELECT 'de', d1.unique_id, d2.unique_id
      FROM datasetlink dl
      JOIN dataset d1 ON d1.id = dl.parent_dataset_id
      JOIN dataset d2 ON d2.id = dl.child_dataset_id
     WHERE dl.parent_dataset_id IN (SELECT id FROM d_neighbors)
       AND dl.child_dataset_id  IN (SELECT id FROM d_neighbors)
    UNION ALL
    SELECT 'sd', s.unique_id, d.unique_id
      FROM datasetsamplelink dsl
      JOIN sample  s ON s.id = dsl.sample_id
      JOIN dataset d ON d.id = dsl.dataset_id
     WHERE dsl.sample_id  IN (SELECT id FROM s_neighbors)
       AND dsl.dataset_id IN (SELECT id FROM d_neighbors)
""")

_SAMPLE_SEED_GRAPH_SQL = text("""
    WITH RECURSIVE
        s_down(id) AS (
            SELECT CAST(:seed_id AS int)
            UNION ALL
            SELECT sl.sample_id FROM samplelink sl JOIN s_down d ON sl.parent_sample_id = d.id
        ),
        s_up(id) AS (
            SELECT CAST(:seed_id AS int)
            UNION ALL
            SELECT sl.parent_sample_id FROM samplelink sl JOIN s_up u ON sl.sample_id = u.id
        ),
        s_ids(id) AS (SELECT id FROM s_down UNION SELECT id FROM s_up),
        d_seed(id) AS (
            SELECT DISTINCT dataset_id FROM datasetsamplelink
            WHERE sample_id IN (SELECT id FROM s_ids)
        ),
        d_down(id) AS (
            SELECT id FROM d_seed
            UNION ALL
            SELECT dl.child_dataset_id FROM datasetlink dl JOIN d_down d ON dl.parent_dataset_id = d.id
        ),
        d_up(id) AS (
            SELECT id FROM d_seed
            UNION ALL
            SELECT dl.parent_dataset_id FROM datasetlink dl JOIN d_up u ON dl.child_dataset_id = u.id
        ),
        d_ids(id) AS (SELECT id FROM d_down UNION SELECT id FROM d_up)
    SELECT 'sn' AS t, s.unique_id AS a, s.sample_name AS b
      FROM sample s WHERE s.id IN (SELECT id FROM s_ids)
    UNION ALL
    SELECT 'dn', d.unique_id, d.dataset_name
      FROM dataset d WHERE d.id IN (SELECT id FROM d_ids)
    UNION ALL
    SELECT 'se', s1.unique_id, s2.unique_id
      FROM samplelink sl
      JOIN sample s1 ON s1.id = sl.parent_sample_id
      JOIN sample s2 ON s2.id = sl.sample_id
     WHERE sl.parent_sample_id IN (SELECT id FROM s_ids)
       AND sl.sample_id       IN (SELECT id FROM s_ids)
    UNION ALL
    SELECT 'de', d1.unique_id, d2.unique_id
      FROM datasetlink dl
      JOIN dataset d1 ON d1.id = dl.parent_dataset_id
      JOIN dataset d2 ON d2.id = dl.child_dataset_id
     WHERE dl.parent_dataset_id IN (SELECT id FROM d_ids)
       AND dl.child_dataset_id  IN (SELECT id FROM d_ids)
    UNION ALL
    SELECT 'sd', s.unique_id, d.unique_id
      FROM datasetsamplelink dsl
      JOIN sample  s ON s.id = dsl.sample_id
      JOIN dataset d ON d.id = dsl.dataset_id
     WHERE dsl.sample_id  IN (SELECT id FROM s_ids)
       AND dsl.dataset_id IN (SELECT id FROM d_ids)
""")

_DATASET_SEED_GRAPH_SQL = text("""
    WITH RECURSIVE
        d1_down(id) AS (
            SELECT CAST(:seed_id AS int)
            UNION ALL
            SELECT dl.child_dataset_id FROM datasetlink dl JOIN d1_down d ON dl.parent_dataset_id = d.id
        ),
        d1_up(id) AS (
            SELECT CAST(:seed_id AS int)
            UNION ALL
            SELECT dl.parent_dataset_id FROM datasetlink dl JOIN d1_up u ON dl.child_dataset_id = u.id
        ),
        d1_ids(id) AS (SELECT id FROM d1_down UNION SELECT id FROM d1_up),
        s_seed(id) AS (
            SELECT DISTINCT sample_id FROM datasetsamplelink
            WHERE dataset_id IN (SELECT id FROM d1_ids)
        ),
        s_down(id) AS (
            SELECT id FROM s_seed
            UNION ALL
            SELECT sl.sample_id FROM samplelink sl JOIN s_down d ON sl.parent_sample_id = d.id
        ),
        s_up(id) AS (
            SELECT id FROM s_seed
            UNION ALL
            SELECT sl.parent_sample_id FROM samplelink sl JOIN s_up u ON sl.sample_id = u.id
        ),
        s_ids(id) AS (SELECT id FROM s_down UNION SELECT id FROM s_up),
        d2_seed(id) AS (
            SELECT DISTINCT dataset_id FROM datasetsamplelink
             WHERE sample_id  IN (SELECT id FROM s_ids)
               AND dataset_id NOT IN (SELECT id FROM d1_ids)
        ),
        d2_down(id) AS (
            SELECT id FROM d2_seed
            UNION ALL
            SELECT dl.child_dataset_id FROM datasetlink dl JOIN d2_down d ON dl.parent_dataset_id = d.id
        ),
        d2_up(id) AS (
            SELECT id FROM d2_seed
            UNION ALL
            SELECT dl.parent_dataset_id FROM datasetlink dl JOIN d2_up u ON dl.child_dataset_id = u.id
        ),
        d_ids(id) AS (
            SELECT id FROM d1_ids
            UNION SELECT id FROM d2_down
            UNION SELECT id FROM d2_up
        )
    SELECT 'sn' AS t, s.unique_id AS a, s.sample_name AS b
      FROM sample s WHERE s.id IN (SELECT id FROM s_ids)
    UNION ALL
    SELECT 'dn', d.unique_id, d.dataset_name
      FROM dataset d WHERE d.id IN (SELECT id FROM d_ids)
    UNION ALL
    SELECT 'se', s1.unique_id, s2.unique_id
      FROM samplelink sl
      JOIN sample s1 ON s1.id = sl.parent_sample_id
      JOIN sample s2 ON s2.id = sl.sample_id
     WHERE sl.parent_sample_id IN (SELECT id FROM s_ids)
       AND sl.sample_id       IN (SELECT id FROM s_ids)
    UNION ALL
    SELECT 'de', d1.unique_id, d2.unique_id
      FROM datasetlink dl
      JOIN dataset d1 ON d1.id = dl.parent_dataset_id
      JOIN dataset d2 ON d2.id = dl.child_dataset_id
     WHERE dl.parent_dataset_id IN (SELECT id FROM d_ids)
       AND dl.child_dataset_id  IN (SELECT id FROM d_ids)
    UNION ALL
    SELECT 'sd', s.unique_id, d.unique_id
      FROM datasetsamplelink dsl
      JOIN sample  s ON s.id = dsl.sample_id
      JOIN dataset d ON d.id = dsl.dataset_id
     WHERE dsl.sample_id  IN (SELECT id FROM s_ids)
       AND dsl.dataset_id IN (SELECT id FROM d_ids)
""")


def _parse_graph_rows(rows) -> dict:
    """Convert tagged mixed result rows into a node-link dict."""
    nodes, links = [], []
    for t, a, b in rows:
        if t == "sn":
            nodes.append({"id": a, "entity_type": "sample",  "name": b})
        elif t == "dn":
            nodes.append({"id": a, "entity_type": "dataset", "name": b})
        else:
            links.append({"source": a, "target": b})
    return {"directed": True, "multigraph": False, "graph": {}, "nodes": nodes, "edges": links}


@router.get("/entity_graph_cte/{entity_id}")
def entity_graph_cte(
    entity_id: str,
    auth_res: Annotated[Principal, Depends(authorize_user)],
    recursive: bool = False,
):
    """
    Returns the graph of entities connected to entity_id.
    By default returns only first-degree neighbours (direct parents, children,
    and cross-linked entities). Pass recursive=true for the full connected component.
    Use /project_graph/{project_id} to get all entities in a project.
    """
    user_unique_id, ag_list = auth_res.user_id, auth_res.groups
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        # --- Auth + identify root (1 query) ---
        sample_target = session.exec(
            select(Sample).where(Sample.unique_id == entity_id)
        ).one_or_none()

        if sample_target is not None:
            acl_ok = session.exec(
                select(ResourceACL)
                .where(ResourceACL.mfid == entity_id)
                .where(ResourceACL.accessgroup_id.in_(ag_ids))
            ).first()
            if acl_ok is None:
                raise HTTPException(status_code=403, detail="Access denied")
            seed_id = sample_target.id
            sql = _SAMPLE_SEED_GRAPH_SQL if recursive else _SAMPLE_SEED_1HOP_SQL
        else:
            auth_filter = (ResourceACL.accessgroup_id.in_(ag_ids)) | (Dataset.public == True)
            dataset_target = session.exec(
                select(Dataset)
                .join(ResourceACL, ResourceACL.mfid == Dataset.unique_id, isouter=True)
                .filter(auth_filter)
                .where(Dataset.unique_id == entity_id)
            ).first()
            if dataset_target is None:
                raise HTTPException(status_code=404, detail="Entity not found")
            seed_id = dataset_target.id
            sql = _DATASET_SEED_GRAPH_SQL if recursive else _DATASET_SEED_1HOP_SQL

        # --- Single query: traversal + nodes + edges ---
        rows = session.execute(sql, {"seed_id": seed_id})
        return _parse_graph_rows(rows)


# ---------------------------------------------------------------------------
# GET /project_graph/{project_id}
# Returns all entities in the project regardless of connectivity.
# ---------------------------------------------------------------------------

@router.get("/project_graph/{project_id}")
def project_graph(
    project_id: str,
    auth_res: Annotated[Principal, Depends(authorize_user)],
):
    """
    Returns all samples and authorized datasets belonging to project_id
    as a single node-link graph.
    """
    ag_list = auth_res.groups
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        sample_ids = set(
            session.exec(select(Sample.id).where(Sample.project_id == project_id)).all()
        )
        auth_filter = (ResourceACL.accessgroup_id.in_(ag_ids)) | (Dataset.public == True)
        dataset_ids = set(
            session.exec(
                select(Dataset.id)
                .join(ResourceACL, ResourceACL.mfid == Dataset.unique_id, isouter=True)
                .filter(auth_filter)
                .where(Dataset.project_id == project_id)
            ).unique().all()
        )
        return _build_graph(session, sample_ids, dataset_ids)

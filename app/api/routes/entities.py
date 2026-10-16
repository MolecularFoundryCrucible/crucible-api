from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import bindparam, text
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.api.path_params import EntityMfid, ProjectRef
from app.models.core_models import (
    Dataset, DatasetLink, DatasetSampleLink, Sample, SampleLink,
)
from app.utils.auth_utils import authorize_user, permission_filter
from app.utils.project_utils import resolve_project_reference
from app.utils.sql_utils import deletion_filter_for
from app.utils.permissions import Permission, Principal, Role, has_global_permission

router = APIRouter(tags=["entities"])
engine = get_engine()


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

    # Parent/child edges carry the link's relationship_type; dataset<->sample
    # association edges have no type, so they report None for a uniform shape.
    if sample_ids:
        sample_edges = session.exec(
            select(SampleLink.parent_sample_id, SampleLink.sample_id, SampleLink.relationship_type)
            .where(SampleLink.parent_sample_id.in_(sample_ids))
            .where(SampleLink.sample_id.in_(sample_ids))
        ).all()
        links += [
            {"source": s_id_to_uid[r[0]], "target": s_id_to_uid[r[1]], "relationship_type": r[2]}
            for r in sample_edges
        ]

    if dataset_ids:
        dataset_edges = session.exec(
            select(DatasetLink.parent_dataset_id, DatasetLink.child_dataset_id, DatasetLink.relationship_type)
            .where(DatasetLink.parent_dataset_id.in_(dataset_ids))
            .where(DatasetLink.child_dataset_id.in_(dataset_ids))
        ).all()
        links += [
            {"source": d_id_to_uid[r[0]], "target": d_id_to_uid[r[1]], "relationship_type": r[2]}
            for r in dataset_edges
        ]

    if sample_ids and dataset_ids:
        sd_edges = session.exec(
            select(DatasetSampleLink.sample_id, DatasetSampleLink.dataset_id)
            .where(DatasetSampleLink.sample_id.in_(sample_ids))
            .where(DatasetSampleLink.dataset_id.in_(dataset_ids))
        ).all()
        links += [
            {"source": s_id_to_uid[r[0]], "target": d_id_to_uid[r[1]], "relationship_type": None}
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

_VISIBLE_ENTITIES_SQL = """
        visible_samples(id) AS (
            SELECT s.id
            FROM sample s
            WHERE NOT EXISTS (
                SELECT 1
                FROM deletionrequest dr
                WHERE dr.resource_id = s.unique_id
                  AND dr.status IN ('pending', 'approved')
            )
              AND (
                  :platform_admin
                  OR EXISTS (
                      SELECT 1
                      FROM resourceacl acl
                      WHERE acl.mfid = s.unique_id
                        AND acl.accessgroup_id IN :group_ids
                        AND acl.permission IN ('viewer', 'contributor', 'editor', 'admin', 'owner')
                  )
              )
        ),
        visible_datasets(id) AS (
            SELECT d.id
            FROM dataset d
            WHERE NOT EXISTS (
                SELECT 1
                FROM deletionrequest dr
                WHERE dr.resource_id = d.unique_id
                  AND dr.status IN ('pending', 'approved')
            )
              AND (
                  :platform_admin
                  OR EXISTS (
                      SELECT 1
                      FROM resourceacl acl
                      WHERE acl.mfid = d.unique_id
                        AND acl.accessgroup_id IN :group_ids
                        AND acl.permission IN ('viewer', 'contributor', 'editor', 'admin', 'owner')
                  )
              )
        )
"""


def _graph_sql(statement: str):
    return text(statement).bindparams(bindparam("group_ids", expanding=True))


_SAMPLE_SEED_1HOP_SQL = _graph_sql(f"""
    WITH
        {_VISIBLE_ENTITIES_SQL},
        s_neighbors(id) AS (
            SELECT CAST(:seed_id AS int)
            UNION
            SELECT sl.sample_id FROM samplelink sl
             WHERE sl.parent_sample_id = CAST(:seed_id AS int)
               AND sl.sample_id IN (SELECT id FROM visible_samples)
            UNION
            SELECT sl.parent_sample_id FROM samplelink sl
             WHERE sl.sample_id = CAST(:seed_id AS int)
               AND sl.parent_sample_id IN (SELECT id FROM visible_samples)
        ),
        d_neighbors(id) AS (
            SELECT DISTINCT dsl.dataset_id FROM datasetsamplelink dsl
             WHERE dsl.sample_id IN (SELECT id FROM s_neighbors)
               AND dsl.dataset_id IN (SELECT id FROM visible_datasets)
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

_DATASET_SEED_1HOP_SQL = _graph_sql(f"""
    WITH
        {_VISIBLE_ENTITIES_SQL},
        d_neighbors(id) AS (
            SELECT CAST(:seed_id AS int)
            UNION
            SELECT dl.child_dataset_id FROM datasetlink dl
             WHERE dl.parent_dataset_id = CAST(:seed_id AS int)
               AND dl.child_dataset_id IN (SELECT id FROM visible_datasets)
            UNION
            SELECT dl.parent_dataset_id FROM datasetlink dl
             WHERE dl.child_dataset_id = CAST(:seed_id AS int)
               AND dl.parent_dataset_id IN (SELECT id FROM visible_datasets)
        ),
        s_neighbors(id) AS (
            SELECT DISTINCT dsl.sample_id FROM datasetsamplelink dsl
             WHERE dsl.dataset_id IN (SELECT id FROM d_neighbors)
               AND dsl.sample_id IN (SELECT id FROM visible_samples)
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

_SAMPLE_SEED_GRAPH_SQL = _graph_sql(f"""
    WITH RECURSIVE
        {_VISIBLE_ENTITIES_SQL},
        s_down(id) AS (
            SELECT CAST(:seed_id AS int)
            UNION
            SELECT sl.sample_id FROM samplelink sl JOIN s_down d ON sl.parent_sample_id = d.id
             WHERE sl.sample_id IN (SELECT id FROM visible_samples)
        ),
        s_up(id) AS (
            SELECT CAST(:seed_id AS int)
            UNION
            SELECT sl.parent_sample_id FROM samplelink sl JOIN s_up u ON sl.sample_id = u.id
             WHERE sl.parent_sample_id IN (SELECT id FROM visible_samples)
        ),
        s_ids(id) AS (SELECT id FROM s_down UNION SELECT id FROM s_up),
        d_seed(id) AS (
            SELECT DISTINCT dataset_id FROM datasetsamplelink
            WHERE sample_id IN (SELECT id FROM s_ids)
              AND dataset_id IN (SELECT id FROM visible_datasets)
        ),
        d_down(id) AS (
            SELECT id FROM d_seed
            UNION
            SELECT dl.child_dataset_id FROM datasetlink dl JOIN d_down d ON dl.parent_dataset_id = d.id
             WHERE dl.child_dataset_id IN (SELECT id FROM visible_datasets)
        ),
        d_up(id) AS (
            SELECT id FROM d_seed
            UNION
            SELECT dl.parent_dataset_id FROM datasetlink dl JOIN d_up u ON dl.child_dataset_id = u.id
             WHERE dl.parent_dataset_id IN (SELECT id FROM visible_datasets)
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

_DATASET_SEED_GRAPH_SQL = _graph_sql(f"""
    WITH RECURSIVE
        {_VISIBLE_ENTITIES_SQL},
        d1_down(id) AS (
            SELECT CAST(:seed_id AS int)
            UNION
            SELECT dl.child_dataset_id FROM datasetlink dl JOIN d1_down d ON dl.parent_dataset_id = d.id
             WHERE dl.child_dataset_id IN (SELECT id FROM visible_datasets)
        ),
        d1_up(id) AS (
            SELECT CAST(:seed_id AS int)
            UNION
            SELECT dl.parent_dataset_id FROM datasetlink dl JOIN d1_up u ON dl.child_dataset_id = u.id
             WHERE dl.parent_dataset_id IN (SELECT id FROM visible_datasets)
        ),
        d1_ids(id) AS (SELECT id FROM d1_down UNION SELECT id FROM d1_up),
        s_seed(id) AS (
            SELECT DISTINCT sample_id FROM datasetsamplelink
            WHERE dataset_id IN (SELECT id FROM d1_ids)
              AND sample_id IN (SELECT id FROM visible_samples)
        ),
        s_down(id) AS (
            SELECT id FROM s_seed
            UNION
            SELECT sl.sample_id FROM samplelink sl JOIN s_down d ON sl.parent_sample_id = d.id
             WHERE sl.sample_id IN (SELECT id FROM visible_samples)
        ),
        s_up(id) AS (
            SELECT id FROM s_seed
            UNION
            SELECT sl.parent_sample_id FROM samplelink sl JOIN s_up u ON sl.sample_id = u.id
             WHERE sl.parent_sample_id IN (SELECT id FROM visible_samples)
        ),
        s_ids(id) AS (SELECT id FROM s_down UNION SELECT id FROM s_up),
        d2_seed(id) AS (
            SELECT DISTINCT dataset_id FROM datasetsamplelink
             WHERE sample_id  IN (SELECT id FROM s_ids)
               AND dataset_id NOT IN (SELECT id FROM d1_ids)
               AND dataset_id IN (SELECT id FROM visible_datasets)
        ),
        d2_down(id) AS (
            SELECT id FROM d2_seed
            UNION
            SELECT dl.child_dataset_id FROM datasetlink dl JOIN d2_down d ON dl.parent_dataset_id = d.id
             WHERE dl.child_dataset_id IN (SELECT id FROM visible_datasets)
        ),
        d2_up(id) AS (
            SELECT id FROM d2_seed
            UNION
            SELECT dl.parent_dataset_id FROM datasetlink dl JOIN d2_up u ON dl.child_dataset_id = u.id
             WHERE dl.parent_dataset_id IN (SELECT id FROM visible_datasets)
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


@router.get("/entity_graph_cte/{entity_mfid}")
def entity_graph_cte(
    entity_id: EntityMfid,
    auth_res: Annotated[Principal, Depends(authorize_user)],
    recursive: bool = False,
):
    """
    Returns the graph of entities connected to entity_id.
    By default returns only first-degree neighbours (direct parents, children,
    and cross-linked entities). Pass recursive=true for the full connected component.
    Use /project_graph/{project_ref} to get all entities in a project.
    """

    with Session(engine) as session:
        sample_target = session.exec(
            select(Sample)
            .where(permission_filter(auth_res, Sample, Permission.READ))
            .where(deletion_filter_for(Sample))
            .where(Sample.unique_id == entity_id)
        ).first()

        if sample_target is not None:
            seed_id = sample_target.id
            sql = _SAMPLE_SEED_GRAPH_SQL if recursive else _SAMPLE_SEED_1HOP_SQL
        else:
            dataset_target = session.exec(
                select(Dataset)
                .where(permission_filter(auth_res, Dataset, Permission.READ))
                .where(deletion_filter_for(Dataset))
                .where(Dataset.unique_id == entity_id)
            ).first()
            if dataset_target is None:
                raise HTTPException(status_code=404, detail="Entity not found")
            seed_id = dataset_target.id
            sql = _DATASET_SEED_GRAPH_SQL if recursive else _DATASET_SEED_1HOP_SQL

        rows = session.execute(sql, {
            "seed_id": seed_id,
            "platform_admin": has_global_permission(auth_res, Permission.READ),
            "group_ids": auth_res.group_ids_at_or_above(Role.VIEWER),
        })
        return _parse_graph_rows(rows)


# ---------------------------------------------------------------------------
# GET /project_graph/{project_ref}
# Returns all entities in the project regardless of connectivity.
# ---------------------------------------------------------------------------

@router.get("/project_graph/{project_ref}")
def project_graph(
    project_ref: ProjectRef,
    auth_res: Annotated[Principal, Depends(authorize_user)],
):
    """
    Returns all authorized samples and datasets belonging to the project
    as a single node-link graph.
    """

    with Session(engine) as session:
        project = resolve_project_reference(project_ref, session)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        project_mfid = project.unique_id
        sample_ids = set(
            session.exec(
                select(Sample.id)
                .where(permission_filter(auth_res, Sample, Permission.READ))
                .where(deletion_filter_for(Sample))
                .where(Sample.project_mfid == project_mfid)
            ).all()
        )
        dataset_ids = set(
            session.exec(
                select(Dataset.id)
                .where(permission_filter(auth_res, Dataset, Permission.READ))
                .where(deletion_filter_for(Dataset))
                .where(Dataset.project_mfid == project_mfid)
            ).unique().all()
        )
        return _build_graph(session, sample_ids, dataset_ids)

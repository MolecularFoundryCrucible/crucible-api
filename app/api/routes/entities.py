from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.api.path_params import EntityMfid, ProjectRef
from app.models.core_models import (
    Dataset, DatasetLink, DatasetSampleLink, Sample, SampleLink,
)
from app.utils.auth_utils import authorize_user, permission_filter
from app.utils.project_utils import resolve_project_reference
from app.utils.sql_utils import deletion_filter_for, select_active
from app.utils.permissions import Permission, Principal

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
# Default: first-degree neighbours only. Pass recursive=true for full traversal.
# ---------------------------------------------------------------------------

_IN_CHUNK = 5000


def _visible_neighbours(session, principal, model, to_column, from_column, ids) -> set:
    """Readable, active `model` ids reached through link rows whose `from_column` is in `ids`."""
    found = set()
    ids = list(ids)
    for start in range(0, len(ids), _IN_CHUNK):
        found.update(session.exec(
            select(to_column)
            .join(model, model.id == to_column)
            .where(from_column.in_(ids[start:start + _IN_CHUNK]))
            .where(permission_filter(principal, model, Permission.READ))
            .where(deletion_filter_for(model))
            .distinct()
        ).all())
    return found


def _closure(session, principal, model, to_column, from_column, seeds) -> set:
    reached, frontier = set(seeds), set(seeds)
    while frontier:
        frontier = _visible_neighbours(session, principal, model, to_column, from_column, frontier) - reached
        reached |= frontier
    return reached


def _sample_lineage(session, principal, seeds, recursive=True) -> set:
    step = _closure if recursive else _visible_neighbours
    down = step(session, principal, Sample, SampleLink.sample_id, SampleLink.parent_sample_id, seeds)
    up = step(session, principal, Sample, SampleLink.parent_sample_id, SampleLink.sample_id, seeds)
    return set(seeds) | down | up


def _dataset_lineage(session, principal, seeds, recursive=True) -> set:
    step = _closure if recursive else _visible_neighbours
    down = step(session, principal, Dataset, DatasetLink.child_dataset_id, DatasetLink.parent_dataset_id, seeds)
    up = step(session, principal, Dataset, DatasetLink.parent_dataset_id, DatasetLink.child_dataset_id, seeds)
    return set(seeds) | down | up


def _linked_datasets(session, principal, sample_ids) -> set:
    return _visible_neighbours(session, principal, Dataset, DatasetSampleLink.dataset_id, DatasetSampleLink.sample_id, sample_ids)


def _linked_samples(session, principal, dataset_ids) -> set:
    return _visible_neighbours(session, principal, Sample, DatasetSampleLink.sample_id, DatasetSampleLink.dataset_id, dataset_ids)


def _entity_graph_ids(session, principal, kind: str, seed_id: int, recursive: bool) -> tuple[set, set]:
    """Sample and dataset ids around a readable root: its lineage, the records linked to that
    lineage, and (recursively) their lineage. Only readable, active records are visited, so the
    traversal never crosses one the caller cannot see."""
    if kind == "sample":
        sample_ids = _sample_lineage(session, principal, {seed_id}, recursive)
        dataset_seeds = _linked_datasets(session, principal, sample_ids)
        dataset_ids = _dataset_lineage(session, principal, dataset_seeds) if recursive else dataset_seeds
        return sample_ids, dataset_ids
    dataset_ids = _dataset_lineage(session, principal, {seed_id}, recursive)
    sample_seeds = _linked_samples(session, principal, dataset_ids)
    if not recursive:
        return sample_seeds, dataset_ids
    sample_ids = _sample_lineage(session, principal, sample_seeds)
    more_datasets = _linked_datasets(session, principal, sample_ids) - dataset_ids
    return sample_ids, dataset_ids | _dataset_lineage(session, principal, more_datasets)


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
        sample_target = session.exec(select_active(Sample, entity_id, principal=auth_res)).first()

        if sample_target is not None:
            kind, seed_id = "sample", sample_target.id
        else:
            dataset_target = session.exec(select_active(Dataset, entity_id, principal=auth_res)).first()
            if dataset_target is None:
                raise HTTPException(status_code=404, detail="Entity not found")
            kind, seed_id = "dataset", dataset_target.id

        sample_ids, dataset_ids = _entity_graph_ids(session, auth_res, kind, seed_id, recursive)
        return _build_graph(session, sample_ids, dataset_ids)


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

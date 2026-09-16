from typing import List, Optional, Sequence

from sqlmodel import Session, select

from app.models.core_models import (
    Dataset,
    DatasetLink,
    DatasetSampleLink,
    Sample,
    SampleLink,
)
from app.models.links import LinkedResource
from app.utils.auth_utils import permission_filter, require_permission
from app.utils.permissions import Permission, Principal
from app.utils.sql_utils import deletion_filter_for


def require_link_create_permissions(
    session: Session,
    principal: Principal,
    source,
    target,
) -> None:
    if (
        source.project_mfid is not None
        and source.project_mfid == target.project_mfid
    ):
        require_permission(session, principal, source.unique_id, Permission.CREATE)
        require_permission(session, principal, target.unique_id, Permission.READ)
        return
    require_permission(session, principal, source.unique_id, Permission.EDIT)
    require_permission(session, principal, target.unique_id, Permission.EDIT)


def fetch_readable_sample_datasets(
    session: Session,
    principal: Principal,
    sample_id: int,
) -> List[Dataset]:
    return list(session.exec(
        select(Dataset)
        .join(DatasetSampleLink, DatasetSampleLink.dataset_id == Dataset.id)
        .where(DatasetSampleLink.sample_id == sample_id)
        .where(permission_filter(principal, Dataset, Permission.READ))
        .where(deletion_filter_for(Dataset))
        .order_by(Dataset.unique_id)
    ).all())


def fetch_readable_links(
    session: Session,
    principal: Principal,
    resource_type: str,
    entity,
    associated_entities: Optional[Sequence] = None,
) -> List[LinkedResource]:
    results: List[LinkedResource] = []

    if resource_type == "dataset":
        # Each parent/child row carries the link's relationship_type alongside
        # the linked dataset; it is None for links created before typing.
        parents = session.exec(
            select(Dataset, DatasetLink.relationship_type)
            .join(DatasetLink, DatasetLink.parent_dataset_id == Dataset.id)
            .where(DatasetLink.child_dataset_id == entity.id)
            .where(permission_filter(principal, Dataset, Permission.READ))
            .where(deletion_filter_for(Dataset))
            .order_by(Dataset.unique_id)
        ).all()
        children = session.exec(
            select(Dataset, DatasetLink.relationship_type)
            .join(DatasetLink, DatasetLink.child_dataset_id == Dataset.id)
            .where(DatasetLink.parent_dataset_id == entity.id)
            .where(permission_filter(principal, Dataset, Permission.READ))
            .where(deletion_filter_for(Dataset))
            .order_by(Dataset.unique_id)
        ).all()
        samples = session.exec(
            select(Sample)
            .join(DatasetSampleLink, DatasetSampleLink.sample_id == Sample.id)
            .where(DatasetSampleLink.dataset_id == entity.id)
            .where(permission_filter(principal, Sample, Permission.READ))
            .where(deletion_filter_for(Sample))
            .order_by(Sample.unique_id)
        ).all()
        results.extend(
            LinkedResource(
                unique_id=dataset.unique_id,
                resource_type="dataset",
                name=dataset.dataset_name,
                direction="source",
                relationship_type=relationship_type,
            )
            for dataset, relationship_type in parents
        )
        results.extend(
            LinkedResource(
                unique_id=dataset.unique_id,
                resource_type="dataset",
                name=dataset.dataset_name,
                direction="target",
                relationship_type=relationship_type,
            )
            for dataset, relationship_type in children
        )
        results.extend(
            LinkedResource(
                unique_id=sample.unique_id,
                resource_type="sample",
                name=sample.sample_name,
                direction="undirected",
            )
            for sample in samples
        )

    elif resource_type == "sample":
        parents = session.exec(
            select(Sample, SampleLink.relationship_type)
            .join(SampleLink, SampleLink.parent_sample_id == Sample.id)
            .where(SampleLink.sample_id == entity.id)
            .where(permission_filter(principal, Sample, Permission.READ))
            .where(deletion_filter_for(Sample))
            .order_by(Sample.unique_id)
        ).all()
        children = session.exec(
            select(Sample, SampleLink.relationship_type)
            .join(SampleLink, SampleLink.sample_id == Sample.id)
            .where(SampleLink.parent_sample_id == entity.id)
            .where(permission_filter(principal, Sample, Permission.READ))
            .where(deletion_filter_for(Sample))
            .order_by(Sample.unique_id)
        ).all()
        datasets = (
            list(associated_entities)
            if associated_entities is not None
            else fetch_readable_sample_datasets(session, principal, entity.id)
        )
        results.extend(
            LinkedResource(
                unique_id=sample.unique_id,
                resource_type="sample",
                name=sample.sample_name,
                direction="source",
                relationship_type=relationship_type,
            )
            for sample, relationship_type in parents
        )
        results.extend(
            LinkedResource(
                unique_id=sample.unique_id,
                resource_type="sample",
                name=sample.sample_name,
                direction="target",
                relationship_type=relationship_type,
            )
            for sample, relationship_type in children
        )
        results.extend(
            LinkedResource(
                unique_id=dataset.unique_id,
                resource_type="dataset",
                name=dataset.dataset_name,
                direction="undirected",
            )
            for dataset in datasets
        )

    return results

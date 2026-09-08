from starlette.requests import Request
from sqlmodel import Session

import app.api.routes.datasets as datasets_routes
import app.api.routes.resources as resources_routes
import app.api.routes.samples as samples_routes
import app.services.resource_details as resource_details
from app.models.core_models import (
    AccessGroup,
    Dataset,
    DatasetLink,
    DatasetSampleLink,
    DeletionRequest,
    Resource,
    ResourceACL,
    Sample,
    SampleLink,
)
from app.utils.link_utils import fetch_readable_links, fetch_readable_sample_datasets
from app.utils.permissions import GroupMembership, PlatformRole, Principal, Role


def _setup(db_engine):
    with Session(db_engine) as session:
        reader_group = AccessGroup(group_name="link-readers")
        private_group = AccessGroup(group_name="link-private")
        session.add_all([reader_group, private_group])
        session.commit()
        session.refresh(reader_group)
        session.refresh(private_group)

        datasets = {
            "root": Dataset(unique_id="dataset-root", dataset_name="dataset root"),
            "visible_parent": Dataset(unique_id="dataset-visible-parent", dataset_name="visible parent"),
            "visible_child": Dataset(unique_id="dataset-visible-child", dataset_name="visible child"),
            "private": Dataset(unique_id="dataset-private", dataset_name="private dataset"),
            "deleted": Dataset(unique_id="dataset-deleted", dataset_name="deleted dataset"),
        }
        samples = {
            "root": Sample(unique_id="sample-root", sample_name="sample root"),
            "visible_parent": Sample(unique_id="sample-visible-parent", sample_name="visible parent"),
            "visible_child": Sample(unique_id="sample-visible-child", sample_name="visible child"),
            "private": Sample(unique_id="sample-private", sample_name="private sample"),
            "deleted": Sample(unique_id="sample-deleted", sample_name="deleted sample"),
        }
        session.add_all([*datasets.values(), *samples.values()])
        session.commit()
        for entity in (*datasets.values(), *samples.values()):
            session.refresh(entity)
            session.add(Resource(
                mfid=entity.unique_id,
                resource_type="dataset" if isinstance(entity, Dataset) else "sample",
            ))

        visible_entities = (
            datasets["root"],
            datasets["visible_parent"],
            datasets["visible_child"],
            datasets["deleted"],
            samples["root"],
            samples["visible_parent"],
            samples["visible_child"],
            samples["deleted"],
        )
        for entity in visible_entities:
            session.add(ResourceACL(
                mfid=entity.unique_id,
                accessgroup_id=reader_group.id,
                read=True,
                write=False,
                permission=Role.VIEWER.db_value,
            ))
        for entity in (datasets["private"], samples["private"]):
            session.add(ResourceACL(
                mfid=entity.unique_id,
                accessgroup_id=private_group.id,
                read=True,
                write=False,
                permission=Role.VIEWER.db_value,
            ))

        session.add_all([
            DatasetLink(
                parent_dataset_id=datasets["visible_parent"].id,
                child_dataset_id=datasets["root"].id,
            ),
            DatasetLink(
                parent_dataset_id=datasets["private"].id,
                child_dataset_id=datasets["root"].id,
            ),
            DatasetLink(
                parent_dataset_id=datasets["root"].id,
                child_dataset_id=datasets["visible_child"].id,
            ),
            DatasetLink(
                parent_dataset_id=datasets["root"].id,
                child_dataset_id=datasets["private"].id,
            ),
            DatasetLink(
                parent_dataset_id=datasets["root"].id,
                child_dataset_id=datasets["deleted"].id,
            ),
            SampleLink(
                parent_sample_id=samples["visible_parent"].id,
                sample_id=samples["root"].id,
            ),
            SampleLink(
                parent_sample_id=samples["private"].id,
                sample_id=samples["root"].id,
            ),
            SampleLink(
                parent_sample_id=samples["root"].id,
                sample_id=samples["visible_child"].id,
            ),
            SampleLink(
                parent_sample_id=samples["root"].id,
                sample_id=samples["private"].id,
            ),
            SampleLink(
                parent_sample_id=samples["root"].id,
                sample_id=samples["deleted"].id,
            ),
            DatasetSampleLink(
                dataset_id=datasets["root"].id,
                sample_id=samples["root"].id,
            ),
            DatasetSampleLink(
                dataset_id=datasets["root"].id,
                sample_id=samples["private"].id,
            ),
            DatasetSampleLink(
                dataset_id=datasets["root"].id,
                sample_id=samples["deleted"].id,
            ),
            DatasetSampleLink(
                dataset_id=datasets["private"].id,
                sample_id=samples["root"].id,
            ),
            DatasetSampleLink(
                dataset_id=datasets["deleted"].id,
                sample_id=samples["root"].id,
            ),
            DeletionRequest(
                resource_id=datasets["deleted"].unique_id,
                requester_id="link-user",
                status="pending",
            ),
            DeletionRequest(
                resource_id=samples["deleted"].unique_id,
                requester_id="link-user",
                status="approved",
            ),
        ])
        session.commit()
        return reader_group.id, datasets["root"].id, samples["root"].id


def _reader(group_id: int) -> Principal:
    return Principal(
        user_id="link-user",
        groups=(GroupMembership(
            id=group_id,
            group_name="link-readers",
            role=Role.VIEWER,
        ),),
    )


def _link_ids(links):
    return {
        (link.unique_id, link.direction)
        for link in links
    }


def _request():
    return Request({"type": "http", "query_string": b"", "headers": []})


def test_singleton_link_helper_filters_each_target(db_engine):
    group_id, dataset_id, sample_id = _setup(db_engine)
    principal = _reader(group_id)
    with Session(db_engine) as session:
        dataset = session.get(Dataset, dataset_id)
        sample = session.get(Sample, sample_id)

        dataset_links = fetch_readable_links(
            session, principal, "dataset", dataset
        )
        sample_links = fetch_readable_links(
            session, principal, "sample", sample
        )
        sample_datasets = fetch_readable_sample_datasets(
            session, principal, sample.id
        )

    assert _link_ids(dataset_links) == {
        ("dataset-visible-parent", "source"),
        ("dataset-visible-child", "target"),
        ("sample-root", "undirected"),
    }
    assert _link_ids(sample_links) == {
        ("sample-visible-parent", "source"),
        ("sample-visible-child", "target"),
        ("dataset-root", "undirected"),
    }
    assert [dataset.unique_id for dataset in sample_datasets] == ["dataset-root"]


def test_singleton_and_generic_routes_filter_expanded_links(
    db_engine,
    monkeypatch,
):
    group_id, _, _ = _setup(db_engine)
    principal = _reader(group_id)
    monkeypatch.setattr(datasets_routes, "engine", db_engine)
    monkeypatch.setattr(samples_routes, "engine", db_engine)
    monkeypatch.setattr(resources_routes, "engine", db_engine)

    dataset = datasets_routes.read_dataset(
        principal,
        "dataset-root",
        include_links=True,
    )
    sample = samples_routes.read_sample(
        "sample-root",
        principal,
        include_links=True,
    )
    generic_sample = resources_routes.get_resource(
        principal,
        "sample-root",
        include_links=True,
    )
    generic_links = resources_routes.get_resource_links(
        "dataset-root",
        principal,
    )

    assert _link_ids(dataset.links) == {
        ("dataset-visible-parent", "source"),
        ("dataset-visible-child", "target"),
        ("sample-root", "undirected"),
    }
    assert _link_ids(sample.links) == {
        ("sample-visible-parent", "source"),
        ("sample-visible-child", "target"),
        ("dataset-root", "undirected"),
    }
    assert [dataset["unique_id"] for dataset in sample.model_dump()["datasets"]] == ["dataset-root"]
    assert [dataset["unique_id"] for dataset in generic_sample.model_dump()["datasets"]] == ["dataset-root"]
    assert dataset.capabilities.model_dump() == {
        "can_edit": False,
        "can_manage_access": False,
        "can_change_status": False,
        "can_transfer": False,
        "max_grant_role": None,
    }
    assert sample.capabilities == dataset.capabilities
    assert generic_sample.capabilities == dataset.capabilities
    assert _link_ids(generic_links) == _link_ids(dataset.links)


def test_singleton_sample_can_omit_embedded_datasets_without_omitting_links(
    db_engine,
    monkeypatch,
):
    group_id, _, _ = _setup(db_engine)
    principal = _reader(group_id)
    monkeypatch.setattr(samples_routes, "engine", db_engine)
    monkeypatch.setattr(resources_routes, "engine", db_engine)

    sample = samples_routes.read_sample(
        "sample-root",
        principal,
        include_links=True,
        include_datasets=False,
    )
    generic_sample = resources_routes.get_resource(
        principal,
        "sample-root",
        include_links=True,
        include_datasets=False,
    )

    assert sample.model_dump()["datasets"] is None
    assert generic_sample.model_dump()["datasets"] is None
    assert _link_ids(sample.links) == {
        ("sample-visible-parent", "source"),
        ("sample-visible-child", "target"),
        ("dataset-root", "undirected"),
    }
    assert _link_ids(generic_sample.links) == _link_ids(sample.links)


def test_singleton_sample_omits_dataset_query_when_expansions_are_disabled(
    db_engine,
    monkeypatch,
):
    group_id, _, _ = _setup(db_engine)
    principal = _reader(group_id)
    monkeypatch.setattr(samples_routes, "engine", db_engine)

    def unexpected_dataset_query(*args, **kwargs):
        raise AssertionError("dataset relationship query should be skipped")

    monkeypatch.setattr(
        resource_details,
        "fetch_readable_sample_datasets",
        unexpected_dataset_query,
    )

    sample = samples_routes.read_sample(
        "sample-root",
        principal,
        include_links=False,
        include_datasets=False,
    )

    assert sample.model_dump()["datasets"] is None
    assert sample.links is None


def test_collection_link_expansion_stays_filtered_and_batched(
    db_engine,
    monkeypatch,
):
    group_id, _, _ = _setup(db_engine)
    principal = _reader(group_id)
    monkeypatch.setattr(datasets_routes, "engine", db_engine)
    monkeypatch.setattr(samples_routes, "engine", db_engine)

    datasets = datasets_routes.list_datasets(
        principal,
        _request(),
        accessible_to_user=None,
        accessible_to_project=None,
        include_metadata=False,
        include_links=True,
        include_owner=False,
        limit=100,
        cursor=None,
        offset=0,
        creation_time_gte=None,
        creation_time_lte=None,
        modification_time_gte=None,
        modification_time_lte=None,
        visibility="all",
    )
    samples = samples_routes.list_samples(
        principal,
        _request(),
        accessible_to_user=None,
        accessible_to_project=None,
        include_metadata=False,
        include_links=True,
        include_datasets=False,
        include_owner=False,
        limit=100,
        cursor=None,
        offset=0,
        creation_time_gte=None,
        creation_time_lte=None,
        modification_time_gte=None,
        modification_time_lte=None,
        visibility="all",
    )

    dataset_root = next(item for item in datasets.items if item.unique_id == "dataset-root")
    sample_root = next(item for item in samples.items if item.unique_id == "sample-root")
    assert dataset_root.capabilities is None
    assert sample_root.capabilities is None
    assert _link_ids(dataset_root.links) == {
        ("dataset-visible-parent", "source"),
        ("dataset-visible-child", "target"),
        ("sample-root", "undirected"),
    }
    assert _link_ids(sample_root.links) == {
        ("sample-visible-parent", "source"),
        ("sample-visible-child", "target"),
        ("dataset-root", "undirected"),
    }


def test_platform_admin_sees_private_but_not_deleted_links(db_engine):
    _, dataset_id, _ = _setup(db_engine)
    principal = Principal(
        user_id="platform-admin",
        platform_role=PlatformRole.ADMIN,
    )
    with Session(db_engine) as session:
        dataset = session.get(Dataset, dataset_id)
        links = fetch_readable_links(session, principal, "dataset", dataset)

    assert ("dataset-private", "source") in _link_ids(links)
    assert ("dataset-private", "target") in _link_ids(links)
    assert ("sample-private", "undirected") in _link_ids(links)
    assert not any("deleted" in unique_id for unique_id, _ in _link_ids(links))

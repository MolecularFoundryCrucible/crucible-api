from sqlmodel import Session, select

from app.models.core_models import AssociatedFile, Dataset, DatasetSampleLink, Resource, Sample
from app.utils.permissions import PlatformRole


def _resource_pair(db_engine, *, project_mfid="project-one"):
    with Session(db_engine) as session:
        dataset = Dataset(
            unique_id="dataset-one",
            dataset_name="Dataset One",
            project_mfid=project_mfid,
        )
        sample = Sample(
            unique_id="sample-one",
            sample_name="Sample One",
            project_mfid=project_mfid,
        )
        session.add_all([
            dataset,
            sample,
            Resource(mfid=dataset.unique_id, resource_type="dataset"),
            Resource(mfid=sample.unique_id, resource_type="sample"),
        ])
        session.commit()


def test_service_account_can_read_but_not_edit_private_resources(client, db_engine, auth_as):
    _resource_pair(db_engine)
    auth_as(
        "service-account",
        groups=[],
        platform_role=PlatformRole.CONTRIBUTOR,
        is_service_account=True,
    )

    listed = client.get("/datasets")
    updated = client.patch("/datasets/dataset-one", json={"dataset_name": "Changed"})

    assert listed.status_code == 200, listed.text
    assert [item["unique_id"] for item in listed.json()["items"]] == ["dataset-one"]
    assert updated.status_code == 403, updated.text


def test_service_account_can_add_a_file_without_updating_an_existing_one(client, db_engine, auth_as):
    _resource_pair(db_engine)
    auth_as(
        "service-account",
        groups=[],
        platform_role=PlatformRole.CONTRIBUTOR,
        is_service_account=True,
    )

    created = client.post(
        "/datasets/dataset-one/files",
        json={"filename": "first.dat", "size": 10, "sha256_hash": "same-hash"},
    )
    repeated = client.post(
        "/datasets/dataset-one/files",
        json={"filename": "changed.dat", "size": 99, "sha256_hash": "same-hash"},
    )

    assert created.status_code == 200, created.text
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["mfid"] == created.json()["mfid"]
    assert repeated.json()["filename"] == "first.dat"
    assert repeated.json()["size"] == 10
    with Session(db_engine) as session:
        assert len(session.exec(select(AssociatedFile)).all()) == 1


def test_service_account_can_link_resources_in_the_same_project(client, db_engine, auth_as):
    _resource_pair(db_engine)
    auth_as(
        "service-account",
        groups=[],
        platform_role=PlatformRole.CONTRIBUTOR,
        is_service_account=True,
    )

    response = client.post("/datasets/dataset-one/samples/sample-one")

    assert response.status_code == 200, response.text
    with Session(db_engine) as session:
        assert session.exec(select(DatasetSampleLink)).one_or_none() is not None


def test_service_account_cannot_link_resources_across_projects(client, db_engine, auth_as):
    _resource_pair(db_engine)
    with Session(db_engine) as session:
        sample = session.exec(select(Sample).where(Sample.unique_id == "sample-one")).one()
        sample.project_mfid = "project-two"
        session.add(sample)
        session.commit()
    auth_as(
        "service-account",
        groups=[],
        platform_role=PlatformRole.CONTRIBUTOR,
        is_service_account=True,
    )

    response = client.post("/datasets/dataset-one/samples/sample-one")

    assert response.status_code == 403, response.text
    with Session(db_engine) as session:
        assert session.exec(select(DatasetSampleLink)).one_or_none() is None

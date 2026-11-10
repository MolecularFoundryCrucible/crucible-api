from sqlmodel import Session, select

from app.tables import AccessGroup, Dataset, Project, Resource, ResourceACL, Sample, User
from app.utils.permissions import PlatformRole
from app.utils.sql_utils import set_public


def test_dataset_creation_rolls_back_when_owner_group_is_missing(
    client,
    db_engine,
    auth_as,
):
    with Session(db_engine) as session:
        session.add(User(
            first_name="Missing",
            last_name="Group",
            email="dataset-owner@example.org",
            unique_id="0000-0001-2345-6789",
        ))
        session.commit()
    auth_as("0000-0001-2345-6789", groups=[])
    response = client.post("/datasets", json={"dataset_name": "missing-owner-group"})
    assert response.status_code == 409

    with Session(db_engine) as session:
        assert session.exec(
            select(Dataset).where(Dataset.dataset_name == "missing-owner-group")
        ).one_or_none() is None
        assert session.exec(select(Resource)).all() == []
        assert session.exec(select(ResourceACL)).all() == []


def test_sample_creation_rolls_back_when_owner_group_is_missing(
    client,
    db_engine,
    auth_as,
):
    with Session(db_engine) as session:
        session.add(User(
            first_name="Missing",
            last_name="Group",
            email="sample-owner@example.org",
            unique_id="0000-0002-3456-7890",
        ))
        session.commit()
    auth_as("0000-0002-3456-7890", groups=[])
    response = client.post("/samples", json={"sample_name": "missing-owner-group"})
    assert response.status_code == 409

    with Session(db_engine) as session:
        assert session.exec(
            select(Sample).where(Sample.sample_name == "missing-owner-group")
        ).one_or_none() is None
        assert session.exec(select(Resource)).all() == []
        assert session.exec(select(ResourceACL)).all() == []


def test_dataset_creation_rolls_back_when_project_group_is_missing(
    client,
    db_engine,
    auth_as,
):
    owner_id = "0000-0003-4567-8901"
    with Session(db_engine) as session:
        owner = User(
            first_name="Project",
            last_name="Owner",
            email="owner@example.org",
            unique_id=owner_id,
        )
        session.add(owner)
        session.add(AccessGroup(group_name=owner_id))
        session.add(Project(
            project_id="missing-project-group",
            organization="LBNL",
            project_lead_orcid=owner_id,
        ))
        session.commit()

    auth_as("platform-admin", groups=[], platform_role=PlatformRole.ADMIN)
    response = client.post("/datasets", json={
        "dataset_name": "project-group-rollback",
        "owner_orcid": owner_id,
        "project_id": "missing-project-group",
    })
    assert response.status_code == 409

    with Session(db_engine) as session:
        assert session.exec(
            select(Dataset).where(Dataset.dataset_name == "project-group-rollback")
        ).one_or_none() is None
        assert session.exec(select(Resource)).all() == []
        assert session.exec(select(ResourceACL)).all() == []


def test_publishing_rolls_back_when_public_group_is_missing(
    client,
    db_engine,
    auth_as,
):
    with Session(db_engine) as session:
        session.add(User(
            first_name="Service",
            last_name="Account",
            email="service-account@example.org",
            unique_id="service-account",
            is_service_account=True,
        ))
        session.commit()
    auth_as(
        "service-account",
        groups=[],
        platform_role=PlatformRole.CONTRIBUTOR,
        is_service_account=True,
    )
    response = client.post("/samples", json={
        "sample_name": "missing-public-group",
        "public": True,
    })
    assert response.status_code == 409

    with Session(db_engine) as session:
        assert session.exec(
            select(Sample).where(Sample.sample_name == "missing-public-group")
        ).one_or_none() is None
        assert session.exec(select(Resource)).all() == []
        assert session.exec(select(ResourceACL)).all() == []


def test_unpublishing_without_public_group_is_a_no_op(db_engine):
    with Session(db_engine) as session:
        set_public(session, "missing-resource", False)
        session.commit()

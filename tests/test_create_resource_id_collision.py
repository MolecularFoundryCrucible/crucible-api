"""POST /datasets and POST /samples accept a client-supplied unique_id (e.g.
one generated client-side by an instrument using the same mfid library).
Before this fix, the collision check only queried the target's own table
(Dataset.unique_id / Sample.unique_id) -- a client-supplied id colliding
with an *existing resource of a different type* slipped past that check
and reached add_or_update(Resource, where=(Resource.mfid == id), ...),
which silently flips the other resource's own idtype row to the new
resource_type in place, corrupting it. Checking the shared idtype table
(Resource.mfid) up front catches both same-type and cross-type collisions
in one query.
"""
from sqlmodel import Session, select

from app.models.core_models import AccessGroup, Dataset, Resource, Sample
from app.utils.permissions import AccessGroupType, PlatformRole


def test_dataset_same_type_collision_is_409(client, db_engine, auth_as):
    with Session(db_engine) as session:
        session.add(Dataset(unique_id="00000000000000000000000001", dataset_name="existing", owner_orcid="0000-0001"))
        session.add(Resource(mfid="00000000000000000000000001", resource_type="dataset"))
        session.add(AccessGroup(
            group_name="0000-0001",
            group_type=AccessGroupType.IDENTITY.value,
        ))
        session.commit()

    auth_as("0000-0001", platform_role=PlatformRole.ADMIN)
    resp = client.post("/datasets", json={"unique_id": "00000000000000000000000009", "dataset_name": "d"})
    # sanity: unrelated id must not collide
    assert resp.status_code == 200, resp.text

    resp = client.post("/datasets", json={"unique_id": "00000000000000000000000001", "dataset_name": "d2"})
    assert resp.status_code == 409
    assert "already exists" in resp.text


def test_dataset_colliding_with_a_sample_is_409_and_does_not_corrupt_it(client, db_engine, auth_as):
    with Session(db_engine) as session:
        session.add(Sample(unique_id="00000000000000000000000002", sample_name="s1", owner_orcid="0000-0002"))
        session.add(Resource(mfid="00000000000000000000000002", resource_type="sample"))
        session.commit()

    auth_as("0000-0002", platform_role=PlatformRole.ADMIN)
    resp = client.post("/datasets", json={"unique_id": "00000000000000000000000002", "dataset_name": "d3"})
    assert resp.status_code == 409
    assert "sample" in resp.text

    with Session(db_engine) as session:
        resource = session.exec(select(Resource).where(Resource.mfid == "00000000000000000000000002")).one()
        assert resource.resource_type == "sample"  # untouched
        assert session.exec(select(Dataset).where(Dataset.unique_id == "00000000000000000000000002")).one_or_none() is None


def test_sample_same_type_collision_is_409(client, db_engine, auth_as):
    with Session(db_engine) as session:
        session.add(Sample(unique_id="00000000000000000000000003", sample_name="existing", owner_orcid="0000-0003"))
        session.add(Resource(mfid="00000000000000000000000003", resource_type="sample"))
        session.commit()

    auth_as("0000-0003", platform_role=PlatformRole.ADMIN)
    resp = client.post("/samples", json={"unique_id": "00000000000000000000000003", "sample_name": "s2"})
    assert resp.status_code == 409
    assert "already exists" in resp.text


def test_sample_colliding_with_a_dataset_is_409_and_does_not_corrupt_it(client, db_engine, auth_as):
    with Session(db_engine) as session:
        session.add(Dataset(unique_id="00000000000000000000000004", dataset_name="d1", owner_orcid="0000-0004"))
        session.add(Resource(mfid="00000000000000000000000004", resource_type="dataset"))
        session.commit()

    auth_as("0000-0004", platform_role=PlatformRole.ADMIN)
    resp = client.post("/samples", json={"unique_id": "00000000000000000000000004", "sample_name": "s3"})
    assert resp.status_code == 409
    assert "dataset" in resp.text

    with Session(db_engine) as session:
        resource = session.exec(select(Resource).where(Resource.mfid == "00000000000000000000000004")).one()
        assert resource.resource_type == "dataset"  # untouched
        assert session.exec(select(Sample).where(Sample.unique_id == "00000000000000000000000004")).one_or_none() is None

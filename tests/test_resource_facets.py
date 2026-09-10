from sqlmodel import Session

from app.models.core_models import (
    AccessGroup,
    Dataset,
    Instrument,
    Project,
    Resource,
    ResourceACL,
    Sample,
    User,
    UserAccessGroupLink,
)
from app.utils.permissions import AccessGroupType, Role


ALICE = "0000-0001-1111-1111"
BOB = "0000-0002-2222-2222"


def _seed_facets(db_engine):
    with Session(db_engine) as session:
        alice = User(
            unique_id=ALICE,
            username="alice",
            first_name="Alice",
            last_name="Scientist",
            email="alice@example.com",
        )
        bob = User(
            unique_id=BOB,
            username="bob",
            first_name="Bob",
            last_name="Scientist",
            email="bob@example.com",
        )
        identity_group = AccessGroup(
            group_name=ALICE,
            group_type=AccessGroupType.IDENTITY.value,
        )
        bob_group = AccessGroup(
            group_name=BOB,
            group_type=AccessGroupType.IDENTITY.value,
        )
        session.add_all([alice, bob, identity_group, bob_group])
        session.commit()
        session.refresh(alice)
        session.refresh(bob)
        session.refresh(identity_group)
        session.refresh(bob_group)
        session.add_all([
            UserAccessGroupLink(
                user_id=alice.id,
                accessgroup_id=identity_group.id,
                role=Role.OWNER.db_value,
            ),
            UserAccessGroupLink(
                user_id=bob.id,
                accessgroup_id=bob_group.id,
                role=Role.OWNER.db_value,
            ),
        ])

        project = Project(
            unique_id="project-facet",
            project_id="facet-project",
            title="Facet Project",
            organization="LBNL",
            project_lead_orcid=ALICE,
        )
        instrument = Instrument(
            unique_id="instrument-facet",
            instrument_id="facet-instrument",
            instrument_name="Facet Instrument",
            owner=ALICE,
            location="Building 1",
        )
        session.add_all([project, instrument])

        datasets = [
            Dataset(
                unique_id="facet-dataset-a",
                dataset_name="A",
                owner_orcid=ALICE,
                project_mfid=project.unique_id,
                instrument_mfid=instrument.unique_id,
                session_name="Session 1",
                measurement="XAS",
                data_format="hdf5",
            ),
            Dataset(
                unique_id="facet-dataset-b",
                dataset_name="B",
                owner_orcid=ALICE,
                project_mfid=project.unique_id,
                instrument_mfid=None,
                session_name=None,
                measurement="XAS",
                data_format="hdf5",
            ),
            Dataset(
                unique_id="facet-dataset-c",
                dataset_name="C",
                owner_orcid=BOB,
                project_mfid=None,
                instrument_mfid=None,
                session_name="Session 2",
                measurement="xas",
                data_format="csv",
            ),
            Dataset(
                unique_id="facet-dataset-d",
                dataset_name="D",
                owner_orcid=None,
                project_mfid=None,
                instrument_mfid=None,
                session_name="",
                measurement=None,
                data_format=None,
            ),
        ]
        samples = [
            Sample(
                unique_id="facet-sample-a",
                sample_name="A",
                sample_type="Powder",
                owner_orcid=ALICE,
                project_mfid=project.unique_id,
            ),
            Sample(
                unique_id="facet-sample-b",
                sample_name="B",
                sample_type="powder",
                owner_orcid=BOB,
                project_mfid=None,
            ),
            Sample(
                unique_id="facet-sample-c",
                sample_name="C",
                sample_type=None,
                owner_orcid=None,
                project_mfid=None,
            ),
        ]
        hidden = Dataset(
            unique_id="facet-dataset-hidden",
            dataset_name="Hidden",
            owner_orcid=BOB,
            measurement="SECRET",
        )
        session.add_all(datasets + samples + [hidden])
        for item in datasets + samples:
            resource_type = "dataset" if isinstance(item, Dataset) else "sample"
            session.add(Resource(mfid=item.unique_id, resource_type=resource_type))
            session.add(
                ResourceACL(
                    mfid=item.unique_id,
                    accessgroup_id=bob_group.id if item.owner_orcid == BOB else identity_group.id,
                    read=True,
                    write=True,
                    permission=Role.OWNER.db_value,
                )
            )
            if item.owner_orcid == BOB:
                session.add(
                    ResourceACL(
                        mfid=item.unique_id,
                        accessgroup_id=identity_group.id,
                        read=True,
                        write=False,
                        permission=Role.VIEWER.db_value,
                    )
                )
        session.add(Resource(mfid=hidden.unique_id, resource_type="dataset"))
        session.add(
            ResourceACL(
                mfid=hidden.unique_id,
                accessgroup_id=bob_group.id,
                read=True,
                write=True,
                permission=Role.OWNER.db_value,
            )
        )
        session.commit()
        return identity_group.id, project.unique_id, instrument.unique_id


def test_dataset_text_facets_preserve_values_and_include_null(client, db_engine, auth_as):
    group_id, _, _ = _seed_facets(db_engine)
    auth_as(ALICE, groups=[(ALICE, group_id, Role.OWNER)])

    response = client.get("/datasets/facets", params={"field": "measurement"})

    assert response.status_code == 200, response.text
    assert response.json() == {
        "field": "measurement",
        "limit": 100,
        "items": [
            {"value": "XAS", "count": 2},
            {"value": "xas", "count": 1},
            {"value": None, "count": 1},
        ],
        "next_cursor": None,
    }
    sessions = client.get("/datasets/facets", params={"field": "session"})
    owned = client.get(
        "/datasets/facets",
        params={"field": "measurement", "affiliation": "owner"},
    )
    assert sessions.status_code == 200, sessions.text
    assert sessions.json()["items"] == [
        {"value": "", "count": 1},
        {"value": "Session 1", "count": 1},
        {"value": "Session 2", "count": 1},
        {"value": None, "count": 1},
    ]
    assert owned.status_code == 200, owned.text
    assert owned.json()["items"] == [
        {"value": "XAS", "count": 2},
        {"value": None, "count": 1},
    ]


def test_dataset_relationship_and_owner_facets_are_resolved(client, db_engine, auth_as):
    group_id, project_mfid, instrument_mfid = _seed_facets(db_engine)
    auth_as(ALICE, groups=[(ALICE, group_id, Role.OWNER)])

    project = client.get("/datasets/facets", params={"field": "project"})
    instrument = client.get("/datasets/facets", params={"field": "instrument"})
    owner = client.get("/datasets/facets", params={"field": "owner"})

    assert project.status_code == 200, project.text
    assert project.json()["items"] == [
        {
            "value": project_mfid,
            "count": 2,
            "project": {
                "unique_id": project_mfid,
                "project_id": "facet-project",
                "title": "Facet Project",
            },
        },
        {"value": None, "count": 2, "project": None},
    ]
    assert instrument.status_code == 200, instrument.text
    assert instrument.json()["items"] == [
        {
            "value": instrument_mfid,
            "count": 1,
            "instrument": {
                "unique_id": instrument_mfid,
                "instrument_id": "facet-instrument",
                "instrument_name": "Facet Instrument",
            },
        },
        {"value": None, "count": 3, "instrument": None},
    ]
    assert owner.status_code == 200, owner.text
    assert owner.json()["items"][-1] == {"value": None, "count": 1, "owner": None}
    assert owner.json()["items"][0]["owner"] == {
        "unique_id": ALICE,
        "username": "alice",
        "first_name": "Alice",
        "last_name": "Scientist",
        "is_service_account": False,
    }


def test_sample_facets_preserve_values_and_resolve_projects(client, db_engine, auth_as):
    group_id, project_mfid, _ = _seed_facets(db_engine)
    auth_as(ALICE, groups=[(ALICE, group_id, Role.OWNER)])

    sample_type = client.get("/samples/facets", params={"field": "sample_type"})
    project = client.get("/samples/facets", params={"field": "project"})

    assert sample_type.status_code == 200, sample_type.text
    assert sample_type.json()["items"] == [
        {"value": "Powder", "count": 1},
        {"value": "powder", "count": 1},
        {"value": None, "count": 1},
    ]
    assert project.status_code == 200, project.text
    assert project.json()["items"][0] == {
        "value": project_mfid,
        "count": 1,
        "project": {
            "unique_id": project_mfid,
            "project_id": "facet-project",
            "title": "Facet Project",
        },
    }
    assert project.json()["items"][1] == {"value": None, "count": 2, "project": None}


def test_facet_cursor_is_bound_to_field_and_returns_null_last(client, db_engine, auth_as):
    group_id, _, _ = _seed_facets(db_engine)
    auth_as(ALICE, groups=[(ALICE, group_id, Role.OWNER)])

    values = []
    cursor = None
    first_cursor = None
    while True:
        params = {"field": "measurement", "limit": 1}
        if cursor is not None:
            params["cursor"] = cursor
        response = client.get("/datasets/facets", params=params)
        assert response.status_code == 200, response.text
        body = response.json()
        values.extend(item["value"] for item in body["items"])
        cursor = body["next_cursor"]
        if first_cursor is None:
            first_cursor = cursor
        if cursor is None:
            break

    assert values == ["XAS", "xas", None]
    invalid = client.get(
        "/datasets/facets",
        params={"field": "data_format", "limit": 1, "cursor": first_cursor},
    )
    assert invalid.status_code == 422


def test_null_facet_buckets_can_be_selected_on_collection_routes(client, db_engine, auth_as):
    group_id, _, _ = _seed_facets(db_engine)
    auth_as(ALICE, groups=[(ALICE, group_id, Role.OWNER)])

    datasets = client.get("/datasets", params={"measurement_is_null": "true"})
    samples = client.get("/samples", params={"sample_type_is_null": "true"})
    assigned = client.get("/datasets", params={"instrument_mfid_is_null": "false"})
    blank_session = client.get("/datasets", params={"session_name": ""})

    assert datasets.status_code == 200, datasets.text
    assert [item["unique_id"] for item in datasets.json()["items"]] == ["facet-dataset-d"]
    assert samples.status_code == 200, samples.text
    assert [item["unique_id"] for item in samples.json()["items"]] == ["facet-sample-c"]
    assert assigned.status_code == 200, assigned.text
    assert [item["unique_id"] for item in assigned.json()["items"]] == ["facet-dataset-a"]
    assert blank_session.status_code == 200, blank_session.text
    assert [item["unique_id"] for item in blank_session.json()["items"]] == ["facet-dataset-d"]


def test_facet_scopes_and_invalid_combinations(client, db_engine, auth_as):
    group_id, project_mfid, _ = _seed_facets(db_engine)
    auth_as(ALICE, groups=[(ALICE, group_id, Role.OWNER)])

    scoped = client.get(
        "/datasets/facets",
        params={"field": "measurement", "project_mfid": project_mfid},
    )
    conflict = client.get(
        "/datasets/facets",
        params={
            "field": "measurement",
            "measurement": "XAS",
            "measurement_is_null": "true",
        },
    )
    unknown = client.get("/samples/facets", params={"field": "measurement"})

    assert scoped.status_code == 200, scoped.text
    assert scoped.json()["items"] == [{"value": "XAS", "count": 2}]
    assert conflict.status_code == 422
    assert unknown.status_code == 422

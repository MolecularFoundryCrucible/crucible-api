from sqlmodel import Session, select

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
from app.models.query_params import DatasetFacetField, DatasetFacetQuery
from app.utils.cursor_utils import filter_fingerprint, new_cursor
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
            {"value": "XAS", "label": "XAS", "count": 2},
            {"value": "xas", "label": "xas", "count": 1},
            {"value": None, "label": None, "count": 1},
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
        {"value": "", "label": "", "count": 1},
        {"value": "Session 1", "label": "Session 1", "count": 1},
        {"value": "Session 2", "label": "Session 2", "count": 1},
        {"value": None, "label": None, "count": 1},
    ]
    assert owned.status_code == 200, owned.text
    assert owned.json()["items"] == [
        {"value": "XAS", "label": "XAS", "count": 2},
        {"value": None, "label": None, "count": 1},
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
            "label": "Facet Project",
            "count": 2,
            "project": {
                "unique_id": project_mfid,
                "project_id": "facet-project",
                "title": "Facet Project",
            },
        },
        {"value": None, "label": None, "count": 2, "project": None},
    ]
    assert instrument.status_code == 200, instrument.text
    assert instrument.json()["items"] == [
        {
            "value": instrument_mfid,
            "label": "Facet Instrument",
            "count": 1,
            "instrument": {
                "unique_id": instrument_mfid,
                "instrument_id": "facet-instrument",
                "instrument_name": "Facet Instrument",
            },
        },
        {"value": None, "label": None, "count": 3, "instrument": None},
    ]
    assert owner.status_code == 200, owner.text
    assert owner.json()["items"][-1] == {
        "value": None,
        "label": None,
        "count": 1,
        "owner": None,
    }
    assert owner.json()["items"][0]["label"] == "Alice Scientist"
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
        {"value": "Powder", "label": "Powder", "count": 1},
        {"value": "powder", "label": "powder", "count": 1},
        {"value": None, "label": None, "count": 1},
    ]
    assert project.status_code == 200, project.text
    assert project.json()["items"][0] == {
        "value": project_mfid,
        "label": "Facet Project",
        "count": 1,
        "project": {
            "unique_id": project_mfid,
            "project_id": "facet-project",
            "title": "Facet Project",
        },
    }
    assert project.json()["items"][1] == {
        "value": None,
        "label": None,
        "count": 2,
        "project": None,
    }


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


def test_existing_default_facet_cursor_context_remains_valid(client, db_engine, auth_as):
    group_id, _, _ = _seed_facets(db_engine)
    auth_as(ALICE, groups=[(ALICE, group_id, Role.OWNER)])
    params = DatasetFacetQuery(field=DatasetFacetField.MEASUREMENT)
    legacy_filters = params.model_dump(
        mode="json",
        exclude={"cursor", "direction", "limit", "sort"},
    )
    cursor = new_cursor(
        kind="dataset_facet",
        sort="measurement",
        direction="asc",
        primary="XAS",
        unique_id="bucket",
        filters=filter_fingerprint(legacy_filters),
        user_id=ALICE,
    )

    response = client.get(
        "/datasets/facets",
        params={"field": "measurement", "cursor": cursor},
    )

    assert response.status_code == 200, response.text
    assert [item["value"] for item in response.json()["items"]] == ["xas", None]


def test_dataset_facets_support_value_descending_order(client, db_engine, auth_as):
    group_id, _, _ = _seed_facets(db_engine)
    auth_as(ALICE, groups=[(ALICE, group_id, Role.OWNER)])

    response = client.get(
        "/datasets/facets",
        params={"field": "measurement", "sort": "value", "direction": "desc"},
    )

    assert response.status_code == 200, response.text
    assert [item["value"] for item in response.json()["items"]] == ["xas", "XAS", None]


def test_dataset_facets_support_label_ordering_and_fallbacks(client, db_engine, auth_as):
    group_id, project_mfid, _ = _seed_facets(db_engine)
    auth_as(ALICE, groups=[(ALICE, group_id, Role.OWNER)])
    with Session(db_engine) as session:
        project = session.exec(
            select(Project).where(Project.unique_id == project_mfid)
        ).one()
        project.title = ""
        dataset = session.exec(
            select(Dataset).where(Dataset.unique_id == "facet-dataset-d")
        ).one()
        dataset.project_mfid = "missing-project"
        session.add_all([project, dataset])
        session.commit()

    items = []
    cursor = None
    while True:
        params = {
            "field": "project",
            "sort": "label",
            "direction": "asc",
            "limit": 1,
        }
        if cursor is not None:
            params["cursor"] = cursor
        response = client.get("/datasets/facets", params=params)
        assert response.status_code == 200, response.text
        body = response.json()
        items.extend(body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert [(item["value"], item["label"]) for item in items] == [
        (project_mfid, "facet-project"),
        ("missing-project", "missing-project"),
        (None, None),
    ]
    assert items[1]["project"] is None

    descending = client.get(
        "/datasets/facets",
        params={"field": "project", "sort": "label", "direction": "desc"},
    )
    case_insensitive_tie = client.get(
        "/datasets/facets",
        params={"field": "measurement", "sort": "label", "direction": "desc"},
    )
    assert descending.status_code == 200, descending.text
    assert [item["value"] for item in descending.json()["items"]] == [
        "missing-project",
        project_mfid,
        None,
    ]
    assert case_insensitive_tie.status_code == 200, case_insensitive_tie.text
    assert [item["value"] for item in case_insensitive_tie.json()["items"]] == [
        "XAS",
        "xas",
        None,
    ]


def test_owner_facet_label_uses_username_for_service_accounts(client, db_engine, auth_as):
    group_id, _, _ = _seed_facets(db_engine)
    auth_as(ALICE, groups=[(ALICE, group_id, Role.OWNER)])
    with Session(db_engine) as session:
        service_account = session.exec(
            select(User).where(User.unique_id == BOB)
        ).one()
        service_account.first_name = ""
        service_account.last_name = ""
        service_account.username = "beamline-uploader"
        service_account.is_service_account = True
        session.add(service_account)
        session.commit()

    response = client.get(
        "/datasets/facets",
        params={"field": "owner", "sort": "label", "direction": "asc"},
    )

    assert response.status_code == 200, response.text
    bucket = next(item for item in response.json()["items"] if item["value"] == BOB)
    assert bucket["label"] == "beamline-uploader"
    assert bucket["owner"]["is_service_account"] is True


def test_count_ordered_dataset_facets_paginate_with_stable_ties(client, db_engine, auth_as):
    group_id, _, _ = _seed_facets(db_engine)
    auth_as(ALICE, groups=[(ALICE, group_id, Role.OWNER)])

    items = []
    cursor = None
    first_cursor = None
    limit = 1
    while True:
        params = {
            "field": "session",
            "sort": "count",
            "direction": "desc",
            "limit": limit,
        }
        if cursor is not None:
            params["cursor"] = cursor
        response = client.get("/datasets/facets", params=params)
        assert response.status_code == 200, response.text
        body = response.json()
        items.extend((item["value"], item["count"]) for item in body["items"])
        cursor = body["next_cursor"]
        if first_cursor is None:
            first_cursor = cursor
            limit = 2
        if cursor is None:
            break

    assert items == [("", 1), ("Session 1", 1), ("Session 2", 1), (None, 1)]

    wrong_direction = client.get(
        "/datasets/facets",
        params={
            "field": "session",
            "sort": "count",
            "direction": "asc",
            "limit": 1,
            "cursor": first_cursor,
        },
    )
    wrong_sort = client.get(
        "/datasets/facets",
        params={
            "field": "session",
            "sort": "value",
            "direction": "desc",
            "limit": 1,
            "cursor": first_cursor,
        },
    )
    assert wrong_direction.status_code == 422
    assert wrong_sort.status_code == 422


def test_count_ordering_honors_direction_and_keeps_null_last(client, db_engine, auth_as):
    group_id, _, instrument_mfid = _seed_facets(db_engine)
    auth_as(ALICE, groups=[(ALICE, group_id, Role.OWNER)])

    for direction, expected in (
        ("desc", [("XAS", 2), ("xas", 1), (None, 1)]),
        ("asc", [("xas", 1), ("XAS", 2), (None, 1)]),
    ):
        items = []
        cursor = None
        while True:
            params = {
                "field": "measurement",
                "sort": "count",
                "direction": direction,
                "limit": 1,
            }
            if cursor is not None:
                params["cursor"] = cursor
            response = client.get("/datasets/facets", params=params)
            assert response.status_code == 200, response.text
            body = response.json()
            items.extend((item["value"], item["count"]) for item in body["items"])
            cursor = body["next_cursor"]
            if cursor is None:
                break
        assert items == expected

    instrument_descending = client.get(
        "/datasets/facets",
        params={"field": "instrument", "sort": "count", "direction": "desc"},
    )
    instrument_ascending = client.get(
        "/datasets/facets",
        params={"field": "instrument", "sort": "count", "direction": "asc"},
    )

    assert instrument_descending.status_code == 200, instrument_descending.text
    assert instrument_ascending.status_code == 200, instrument_ascending.text
    expected_instruments = [(instrument_mfid, 1), (None, 3)]
    assert [
        (item["value"], item["count"])
        for item in instrument_descending.json()["items"]
    ] == expected_instruments
    assert [
        (item["value"], item["count"])
        for item in instrument_ascending.json()["items"]
    ] == expected_instruments


def test_sample_facets_support_count_ordering(client, db_engine, auth_as):
    group_id, _, _ = _seed_facets(db_engine)
    auth_as(ALICE, groups=[(ALICE, group_id, Role.OWNER)])

    response = client.get(
        "/samples/facets",
        params={"field": "sample_type", "sort": "count", "direction": "desc"},
    )

    assert response.status_code == 200, response.text
    assert [(item["value"], item["count"]) for item in response.json()["items"]] == [
        ("Powder", 1),
        ("powder", 1),
        (None, 1),
    ]


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
    assert scoped.json()["items"] == [{"value": "XAS", "label": "XAS", "count": 2}]
    assert conflict.status_code == 422
    assert unknown.status_code == 422

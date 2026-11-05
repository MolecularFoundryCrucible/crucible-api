from datetime import datetime

from sqlalchemy import func, literal, or_, update
from sqlmodel import Session

import app.api.routes.datasets as datasets_routes
import app.api.routes.instruments as instruments_routes
import app.api.routes.projects as projects_routes
import app.api.routes.samples as samples_routes

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


def _dt(value):
    return datetime.fromisoformat(value) if value is not None else None


def _seed_owned_resources(db_engine):
    with Session(db_engine) as session:
        alice = User(unique_id=ALICE, first_name="Alice", last_name="Owner", email="alice@example.com")
        bob = User(unique_id=BOB, first_name="Bob", last_name="Owner", email="bob@example.com")
        alice_group = AccessGroup(group_name=ALICE, group_type=AccessGroupType.IDENTITY.value)
        bob_group = AccessGroup(group_name=BOB, group_type=AccessGroupType.IDENTITY.value)
        session.add_all([alice, bob, alice_group, bob_group])
        session.commit()
        session.refresh(alice)
        session.refresh(bob)
        session.refresh(alice_group)
        session.refresh(bob_group)
        session.add_all([
            UserAccessGroupLink(user_id=alice.id, accessgroup_id=alice_group.id, role=Role.OWNER.db_value),
            UserAccessGroupLink(user_id=bob.id, accessgroup_id=bob_group.id, role=Role.OWNER.db_value),
        ])

        alice_project = Project(project_id="alice-project", organization="LBNL", project_lead_orcid=ALICE)
        bob_project = Project(project_id="bob-project", organization="LBNL", project_lead_orcid=BOB)
        session.add_all([alice_project, bob_project])
        session.commit()
        session.refresh(alice_project)
        session.refresh(bob_project)
        alice_project_group = AccessGroup(group_name=alice_project.unique_id, group_type=AccessGroupType.PROJECT.value)
        bob_project_group = AccessGroup(group_name=bob_project.unique_id, group_type=AccessGroupType.PROJECT.value)
        session.add_all([alice_project_group, bob_project_group])
        session.commit()
        session.refresh(alice_project_group)
        session.refresh(bob_project_group)
        session.add_all([
            UserAccessGroupLink(user_id=alice.id, accessgroup_id=alice_project_group.id, role=Role.OWNER.db_value),
            UserAccessGroupLink(user_id=bob.id, accessgroup_id=bob_project_group.id, role=Role.OWNER.db_value),
            UserAccessGroupLink(user_id=alice.id, accessgroup_id=bob_project_group.id, role=Role.VIEWER.db_value),
        ])

        resources = [
            Dataset(unique_id="dataset-alice", dataset_name="Owned search dataset", owner_orcid=ALICE),
            Dataset(unique_id="dataset-bob", dataset_name="Shared search dataset", owner_orcid=BOB),
            Sample(unique_id="sample-alice", sample_name="Owned search sample", owner_orcid=ALICE),
            Sample(unique_id="sample-bob", sample_name="Shared search sample", owner_orcid=BOB),
            Instrument(unique_id="instrument-alice", instrument_id="alice-inst", instrument_name="Owned search instrument", owner=ALICE, owner_user_id=alice.id, location="A"),
            Instrument(unique_id="instrument-bob", instrument_id="bob-inst", instrument_name="Shared search instrument", owner=BOB, owner_user_id=bob.id, location="B"),
        ]
        session.add_all(resources)
        session.add_all([
            Resource(mfid="dataset-alice", resource_type="dataset", creation_time=_dt("2026-01-01T00:00:00+00:00"), modification_time=_dt("2026-01-01T00:00:00+00:00")),
            Resource(mfid="dataset-bob", resource_type="dataset", creation_time=_dt("2026-01-02T00:00:00+00:00"), modification_time=_dt("2026-01-02T00:00:00+00:00")),
            Resource(mfid="sample-alice", resource_type="sample", creation_time=_dt("2026-01-01T00:00:00+00:00"), modification_time=_dt("2026-01-20T00:00:00+00:00")),
            Resource(mfid="sample-bob", resource_type="sample", creation_time=_dt("2026-01-02T00:00:00+00:00"), modification_time=_dt("2026-02-01T00:00:00+00:00")),
            Resource(mfid="instrument-alice", resource_type="instrument", creation_time=_dt("2026-01-01T00:00:00+00:00")),
            Resource(mfid="instrument-bob", resource_type="instrument", creation_time=_dt("2026-01-02T00:00:00+00:00")),
            Resource(mfid=alice_project.unique_id, resource_type="project"),
            Resource(mfid=bob_project.unique_id, resource_type="project"),
        ])
        session.add_all([
            ResourceACL(mfid="dataset-alice", accessgroup_id=alice_group.id, read=True, write=True, permission=Role.OWNER.db_value),
            ResourceACL(mfid="dataset-bob", accessgroup_id=bob_group.id, read=True, write=True, permission=Role.OWNER.db_value),
            ResourceACL(mfid="dataset-bob", accessgroup_id=alice_group.id, read=True, write=False, permission=Role.VIEWER.db_value),
            ResourceACL(mfid="sample-alice", accessgroup_id=alice_group.id, read=True, write=True, permission=Role.OWNER.db_value),
            ResourceACL(mfid="sample-bob", accessgroup_id=bob_group.id, read=True, write=True, permission=Role.OWNER.db_value),
            ResourceACL(mfid="sample-bob", accessgroup_id=alice_group.id, read=True, write=False, permission=Role.VIEWER.db_value),
            ResourceACL(mfid="instrument-alice", accessgroup_id=alice_group.id, read=True, write=True, permission=Role.OWNER.db_value),
            ResourceACL(mfid="instrument-bob", accessgroup_id=bob_group.id, read=True, write=True, permission=Role.OWNER.db_value),
            ResourceACL(mfid=alice_project.unique_id, accessgroup_id=alice_project_group.id, read=True, write=True, permission=Role.OWNER.db_value),
            ResourceACL(mfid=bob_project.unique_id, accessgroup_id=bob_project_group.id, read=True, write=True, permission=Role.OWNER.db_value),
        ])
        session.commit()
        return alice_group.id


def _seed_sortable_datasets(db_engine, group_id):
    rows = [
        ("sort-d", "beta", "2026-01-04T00:00:00+00:00", "2026-01-04T00:00:00+00:00"),
        ("sort-c", "Alpha", "2026-01-03T00:00:00+00:00", "2026-02-01T00:00:00+00:00"),
        ("sort-b", "alpha", "2026-01-02T00:00:00+00:00", "2026-01-20T00:00:00+00:00"),
        ("sort-a", None, "2026-01-05T00:00:00+00:00", "2026-03-01T00:00:00+00:00"),
    ]
    with Session(db_engine) as session:
        for unique_id, name, created, updated in rows:
            session.add(Dataset(unique_id=unique_id, dataset_name=name, owner_orcid=ALICE))
            session.add(Resource(mfid=unique_id, resource_type="dataset", creation_time=_dt(created), modification_time=_dt(updated)))
            session.add(ResourceACL(mfid=unique_id, accessgroup_id=group_id, read=True, write=True, permission=Role.OWNER.db_value))
        session.commit()
        session.exec(update(Dataset).where(Dataset.unique_id == "sort-a").values(dataset_name=None))
        session.commit()


def _ids(response):
    return [item["unique_id"] for item in response.json()["items"]]


def test_affiliation_owner_filters_all_resource_collections(client, db_engine, auth_as):
    group_id = _seed_owned_resources(db_engine)
    auth_as(ALICE, groups=[(ALICE, group_id, Role.OWNER)])

    expected = {
        "/datasets": ["dataset-alice"],
        "/samples": ["sample-alice"],
        "/instruments": ["instrument-alice"],
        "/projects": ["alice-project"],
    }
    for path, identifiers in expected.items():
        response = client.get(path, params={"affiliation": "owner"})
        assert response.status_code == 200, response.text
        field = "project_id" if path == "/projects" else "unique_id"
        assert [item[field] for item in response.json()["items"]] == identifiers


def test_unknown_collection_and_search_parameters_are_rejected(client, db_engine, auth_as):
    group_id = _seed_owned_resources(db_engine)
    auth_as(ALICE, groups=[(ALICE, group_id, Role.OWNER)])

    for path in ("/datasets", "/samples", "/instruments", "/projects"):
        response = client.get(path, params={"affilition": "owner"})
        assert response.status_code == 422, (path, response.text)
    for path in ("/datasets/search", "/samples/search", "/instruments/search", "/projects/search"):
        response = client.get(path, params={"q": "search", "affilition": "owner"})
        assert response.status_code == 422, (path, response.text)

    for path in (
        "/datasets/dataset-alice",
        "/samples/sample-alice",
        "/instruments/instrument-alice",
        "/projects/alice-project",
        "/resources/dataset-alice",
    ):
        response = client.get(path, params={"include_metdata": "true"})
        assert response.status_code == 422, (path, response.text)


def test_affiliation_owner_filters_search_endpoints(client, db_engine, auth_as, monkeypatch):
    group_id = _seed_owned_resources(db_engine)
    auth_as(ALICE, groups=[(ALICE, group_id, Role.OWNER)])

    def apply_search(query, tokens, fields, prefix_fields=()):
        for token in tokens:
            query = query.where(or_(*(func.lower(field).contains(token) for field in fields)))
        return query

    for module in (datasets_routes, samples_routes, instruments_routes, projects_routes):
        monkeypatch.setattr(module, "configure_trigram_search", lambda session, threshold: None)
        monkeypatch.setattr(module, "apply_trigram_search", apply_search)
        monkeypatch.setattr(module, "trigram_rank", lambda query, fields: literal(1))

    expected = {
        "/datasets/search": ("search", ["dataset-alice"]),
        "/samples/search": ("search", ["sample-alice"]),
        "/instruments/search": ("search", ["instrument-alice"]),
        "/projects/search": ("project", ["alice-project"]),
    }
    for path, (term, identifiers) in expected.items():
        response = client.get(path, params={"q": term, "affiliation": "owner"})
        assert response.status_code == 200, (path, response.text)
        field = "project_id" if path == "/projects/search" else "unique_id"
        assert [item[field] for item in response.json()["items"]] == identifiers

    for path, term, identifier in (
        ("/datasets/search", "search", "dataset-bob"),
        ("/samples/search", "search", "sample-bob"),
        ("/instruments/search", "search", "instrument-bob"),
    ):
        response = client.get(path, params={"q": term, "owner_id": BOB})
        assert response.status_code == 200, (path, response.text)
        assert _ids(response) == [identifier]


def test_dataset_sorts_are_deterministic_and_nulls_last(client, db_engine, auth_as):
    group_id = _seed_owned_resources(db_engine)
    _seed_sortable_datasets(db_engine, group_id)
    auth_as(ALICE, groups=[(ALICE, group_id, Role.OWNER)])

    expected = {
        ("created", "asc"): ["dataset-alice", "dataset-bob", "sort-b", "sort-c", "sort-d", "sort-a"],
        ("created", "desc"): ["sort-a", "sort-d", "sort-c", "sort-b", "dataset-bob", "dataset-alice"],
        ("updated", "asc"): ["dataset-alice", "dataset-bob", "sort-d", "sort-b", "sort-c", "sort-a"],
        ("updated", "desc"): ["sort-a", "sort-c", "sort-b", "sort-d", "dataset-bob", "dataset-alice"],
        ("name", "asc"): ["sort-b", "sort-c", "sort-d", "dataset-alice", "dataset-bob", "sort-a"],
        ("name", "desc"): ["dataset-bob", "dataset-alice", "sort-d", "sort-c", "sort-b", "sort-a"],
    }
    for (sort, direction), identifiers in expected.items():
        response = client.get("/datasets", params={"sort": sort, "direction": direction})
        assert response.status_code == 200, response.text
        assert _ids(response) == identifiers

    offset = client.get("/datasets", params={"sort": "name", "direction": "asc", "offset": 2, "limit": 2})
    assert offset.status_code == 200, offset.text
    assert _ids(offset) == expected[("name", "asc")][2:4]


def test_sample_sorts_support_both_directions(client, db_engine, auth_as):
    group_id = _seed_owned_resources(db_engine)
    auth_as(ALICE, groups=[(ALICE, group_id, Role.OWNER)])

    for sort in ("created", "updated", "name"):
        ascending = client.get("/samples", params={"sort": sort, "direction": "asc"})
        descending = client.get("/samples", params={"sort": sort, "direction": "desc"})
        assert ascending.status_code == 200, ascending.text
        assert descending.status_code == 200, descending.text
        assert _ids(ascending) == ["sample-alice", "sample-bob"]
        assert _ids(descending) == ["sample-bob", "sample-alice"]


def test_sorted_cursor_is_opaque_bound_and_complete(client, db_engine, auth_as):
    group_id = _seed_owned_resources(db_engine)
    _seed_sortable_datasets(db_engine, group_id)
    auth_as(ALICE, groups=[(ALICE, group_id, Role.OWNER)])
    params = {"sort": "created", "direction": "asc", "limit": 2}

    first = client.get("/datasets", params=params)
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["total"] == 6
    assert first_body["next_cursor"].startswith("c1.")

    second = client.get("/datasets", params={**params, "cursor": first_body["next_cursor"]})
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert second_body["total"] is None

    third = client.get("/datasets", params={**params, "cursor": second_body["next_cursor"]})
    assert third.status_code == 200, third.text
    assert third.json()["next_cursor"] is None
    assert _ids(first) + _ids(second) + _ids(third) == [
        "dataset-alice", "dataset-bob", "sort-b", "sort-c", "sort-d", "sort-a",
    ]

    changed = client.get("/datasets", params={**params, "visibility": "public", "cursor": first_body["next_cursor"]})
    assert changed.status_code == 422
    tampered_cursor = first_body["next_cursor"][:-8] + ("A" if first_body["next_cursor"][-8] != "A" else "B") + first_body["next_cursor"][-7:]
    tampered = client.get("/datasets", params={**params, "cursor": tampered_cursor})
    assert tampered.status_code == 422

    auth_as(BOB, groups=[])
    other_user = client.get("/datasets", params={**params, "cursor": first_body["next_cursor"]})
    assert other_user.status_code == 422


def test_keyset_pagination_matches_every_explicit_order(client, db_engine, auth_as):
    group_id = _seed_owned_resources(db_engine)
    _seed_sortable_datasets(db_engine, group_id)
    auth_as(ALICE, groups=[(ALICE, group_id, Role.OWNER)])

    for sort in ("created", "updated", "name"):
        for direction in ("asc", "desc"):
            params = {"sort": sort, "direction": direction}
            expected = _ids(client.get("/datasets", params=params))
            actual = []
            cursor = None
            while True:
                page_params = {**params, "limit": 1}
                if cursor is not None:
                    page_params["cursor"] = cursor
                response = client.get("/datasets", params=page_params)
                assert response.status_code == 200, response.text
                actual.extend(_ids(response))
                cursor = response.json()["next_cursor"]
                if cursor is None:
                    break
            assert actual == expected


def test_dataset_anchor_is_inclusive_and_uses_deterministic_sorting(client, db_engine, auth_as):
    group_id = _seed_owned_resources(db_engine)
    _seed_sortable_datasets(db_engine, group_id)
    auth_as(ALICE, groups=[(ALICE, group_id, Role.OWNER)])

    ascending = client.get("/datasets", params={
        "anchor_mfid": "sort-c",
        "sort": "name",
        "direction": "asc",
        "limit": 2,
    })
    assert ascending.status_code == 200, ascending.text
    assert _ids(ascending) == ["sort-c", "sort-d"]
    assert ascending.json()["total"] == 6

    continuation = client.get("/datasets", params={
        "sort": "name",
        "direction": "asc",
        "limit": 2,
        "cursor": ascending.json()["next_cursor"],
    })
    assert continuation.status_code == 200, continuation.text
    assert _ids(continuation) == ["dataset-alice", "dataset-bob"]

    descending = client.get("/datasets", params={
        "anchor_mfid": "sort-c",
        "sort": "name",
        "direction": "desc",
        "limit": 2,
    })
    assert descending.status_code == 200, descending.text
    assert _ids(descending) == ["sort-c", "sort-b"]

    null_anchor = client.get("/datasets", params={
        "anchor_mfid": "sort-a",
        "sort": "name",
        "direction": "asc",
    })
    assert null_anchor.status_code == 200, null_anchor.text
    assert _ids(null_anchor) == ["sort-a"]


def test_sample_anchor_supports_both_directions(client, db_engine, auth_as):
    group_id = _seed_owned_resources(db_engine)
    auth_as(ALICE, groups=[(ALICE, group_id, Role.OWNER)])

    ascending = client.get("/samples", params={
        "anchor_mfid": "sample-alice",
        "sort": "created",
        "direction": "asc",
    })
    descending = client.get("/samples", params={
        "anchor_mfid": "sample-bob",
        "sort": "created",
        "direction": "desc",
    })
    assert ascending.status_code == 200, ascending.text
    assert descending.status_code == 200, descending.text
    assert _ids(ascending) == ["sample-alice", "sample-bob"]
    assert _ids(descending) == ["sample-bob", "sample-alice"]


def test_anchor_rejects_other_page_positions_and_hides_excluded_resources(client, db_engine, auth_as):
    group_id = _seed_owned_resources(db_engine)
    auth_as(ALICE, groups=[(ALICE, group_id, Role.OWNER)])

    for path, anchor, wrong_type in (
        ("/datasets", "dataset-bob", "sample-alice"),
        ("/samples", "sample-bob", "dataset-alice"),
    ):
        for params in (
            {"anchor_mfid": anchor, "cursor": anchor},
            {"anchor_mfid": anchor, "offset": 0},
            {"anchor_mfid": anchor, "offset": 1},
        ):
            response = client.get(path, params=params)
            assert response.status_code == 422, (path, params, response.text)

        for params in (
            {"anchor_mfid": "missing"},
            {"anchor_mfid": anchor, "affiliation": "owner"},
            {"anchor_mfid": wrong_type},
        ):
            response = client.get(path, params=params)
            assert response.status_code == 404, (path, params, response.text)

    auth_as(BOB, groups=[])
    for path, anchor in (("/datasets", "dataset-alice"), ("/samples", "sample-alice")):
        inaccessible = client.get(path, params={"anchor_mfid": anchor})
        assert inaccessible.status_code == 404, (path, inaccessible.text)


def test_anchor_mfid_is_documented_only_for_dataset_and_sample_collections(client):
    paths = client.get("/openapi.json").json()["paths"]
    for path in ("/datasets", "/samples"):
        parameters = {
            parameter["name"]: parameter
            for parameter in paths[path]["get"]["parameters"]
        }
        anchor = parameters["anchor_mfid"]
        assert anchor["required"] is False
        assert "inclusive" in anchor["description"]

    for path in ("/projects", "/instruments"):
        names = {
            parameter["name"]
            for parameter in paths[path]["get"]["parameters"]
        }
        assert "anchor_mfid" not in names


def test_cursor_normalizes_equivalent_time_filters(client, db_engine, auth_as):
    group_id = _seed_owned_resources(db_engine)
    auth_as(ALICE, groups=[(ALICE, group_id, Role.OWNER)])

    first = client.get("/datasets", params={
        "sort": "created",
        "limit": 1,
        "creation_time_gte": "2025-12-31T16:00:00-08:00",
    })
    assert first.status_code == 200, first.text
    continuation = client.get("/datasets", params={
        "sort": "created",
        "limit": 1,
        "creation_time_gte": "2026-01-01T00:00:00+00:00",
        "cursor": first.json()["next_cursor"],
    })
    assert continuation.status_code == 200, continuation.text


def test_query_parameter_conflicts_and_time_validation_return_422(client, db_engine, auth_as):
    group_id = _seed_owned_resources(db_engine)
    auth_as(ALICE, groups=[(ALICE, group_id, Role.OWNER)])

    cases = [
        {"direction": "asc"},
        {"sort": "name", "cursor": "dataset-alice"},
        {"affiliation": "owner", "owner_orcid": ALICE},
        {"affiliation": "owner", "owner_id": ALICE},
        {"owner_id": ALICE, "owner_orcid": BOB},
        {"owner_id": ""},
        {"creation_time_gte": "2026-02-01T00:00:00+00:00", "creation_time_lte": "2026-01-01T00:00:00+00:00"},
        [("accessible_to_user", "")],
        [("affiliation", "maintainer")],
    ]
    for params in cases:
        response = client.get("/datasets", params=params)
        assert response.status_code == 422, (params, response.text)


def test_times_without_an_offset_are_utc(client, db_engine, auth_as):
    group_id = _seed_owned_resources(db_engine)
    auth_as(ALICE, groups=[(ALICE, group_id, Role.OWNER)])

    def ids(value):
        response = client.get("/datasets", params={"creation_time_gte": value})
        assert response.status_code == 200, response.text
        return [item["unique_id"] for item in response.json()["items"]]

    assert ids("2026-01-01T00:00:00") == ids("2026-01-01T00:00:00Z") == ids("2026-01-01T02:00:00+02:00")


def test_cursor_takes_precedence_over_offset(client, db_engine, auth_as):
    group_id = _seed_owned_resources(db_engine)
    auth_as(ALICE, groups=[(ALICE, group_id, Role.OWNER)])

    for path, expected in (
        ("/datasets", ["dataset-alice"]),
        ("/samples", ["sample-alice"]),
    ):
        first = client.get(path, params={"limit": 1})
        assert first.status_code == 200, first.text
        continuation = client.get(path, params={
            "limit": 1,
            "cursor": first.json()["next_cursor"],
            "offset": 1000,
        })
        assert continuation.status_code == 200, continuation.text
        assert _ids(continuation) == expected
        assert continuation.json()["offset"] is None


def test_include_total_controls_count_without_changing_cursor(client, db_engine, auth_as):
    group_id = _seed_owned_resources(db_engine)
    auth_as(ALICE, groups=[(ALICE, group_id, Role.OWNER)])

    for path in ("/datasets", "/samples"):
        default_page = client.get(path, params={"limit": 1})
        assert default_page.status_code == 200, default_page.text
        assert default_page.json()["total"] == 2

        suppressed = client.get(path, params={"limit": 1, "include_total": False})
        assert suppressed.status_code == 200, suppressed.text
        assert suppressed.json()["total"] is None

        deep_offset = client.get(path, params={"limit": 1, "offset": 1})
        assert deep_offset.status_code == 200, deep_offset.text
        assert deep_offset.json()["total"] is None

        requested = client.get(path, params={
            "limit": 1,
            "cursor": default_page.json()["next_cursor"],
            "include_total": True,
        })
        assert requested.status_code == 200, requested.text
        assert requested.json()["total"] == 2


def test_include_total_can_suppress_project_and_instrument_counts(client, db_engine, auth_as):
    group_id = _seed_owned_resources(db_engine)
    auth_as(ALICE, groups=[(ALICE, group_id, Role.OWNER)])

    for path in ("/projects", "/instruments"):
        response = client.get(path, params={"include_total": False})
        assert response.status_code == 200, response.text
        assert response.json()["total"] is None


def test_project_and_instrument_collections_support_compatible_cursors(client, db_engine, auth_as):
    group_id = _seed_owned_resources(db_engine)
    with Session(db_engine) as session:
        session.add(Instrument(
            unique_id="instrument-null",
            instrument_id="null-created",
            instrument_name="Null created instrument",
            owner=ALICE,
            location="C",
        ))
        session.add(Resource(mfid="instrument-null", resource_type="instrument"))
        session.commit()
    auth_as(ALICE, groups=[(ALICE, group_id, Role.OWNER)])

    cases = (
        ("/projects", {"affiliation": "owner"}),
        ("/instruments", {"status": "maintenance"}),
    )
    for path, changed_filter in cases:
        expected = _ids(client.get(path))
        actual = []
        cursor = None
        first_cursor = None
        for page_number in range(len(expected)):
            params = {"limit": 1, "include_total": page_number == 0}
            if cursor is not None:
                params.update({"cursor": cursor, "offset": 999999})
            response = client.get(path, params=params)
            assert response.status_code == 200, (path, response.text)
            body = response.json()
            actual.extend(item["unique_id"] for item in body["items"])
            assert body["offset"] == (0 if page_number == 0 else None)
            assert body["total"] == (len(expected) if page_number == 0 else None)
            cursor = body["next_cursor"]
            if page_number == 0:
                first_cursor = cursor
                assert first_cursor.startswith("c1.")
                assert len(first_cursor) < 160

        assert actual == expected
        assert cursor is None
        changed = client.get(path, params={"cursor": first_cursor, **changed_filter})
        assert changed.status_code == 422, (path, changed.text)
        legacy = client.get(path, params={"cursor": expected[0]})
        assert legacy.status_code == 422, (path, legacy.text)


def test_default_cursor_accepts_legacy_values_and_emits_signed_values(client, db_engine, auth_as):
    group_id = _seed_owned_resources(db_engine)
    auth_as(ALICE, groups=[(ALICE, group_id, Role.OWNER)])

    legacy = client.get("/datasets", params={"cursor": "dataset-bob", "limit": 1})
    assert legacy.status_code == 200, legacy.text
    assert _ids(legacy) == ["dataset-alice"]

    first = client.get("/datasets", params={"limit": 1})
    assert first.status_code == 200, first.text
    assert first.json()["next_cursor"].startswith("c1.")
    continuation = client.get("/datasets", params={
        "limit": 2,
        "include_owner": True,
        "cursor": first.json()["next_cursor"],
    })
    assert continuation.status_code == 200, continuation.text
    assert continuation.json()["next_cursor"] is None

    repeated = client.get("/datasets", params=[("affiliation", "owner"), ("affiliation", "owner")])
    assert repeated.status_code == 200, repeated.text
    assert _ids(repeated) == ["dataset-alice"]


def test_legacy_exact_filters_remain_available(client, db_engine, auth_as):
    group_id = _seed_owned_resources(db_engine)
    auth_as(ALICE, groups=[(ALICE, group_id, Role.OWNER)])

    dataset = client.get("/datasets", params={"dataset_name": "Owned search dataset"})
    sample = client.get("/samples", params={"sample_name": "Owned search sample"})
    owner = client.get("/datasets", params={"owner_orcid": BOB})

    assert _ids(dataset) == ["dataset-alice"]
    assert _ids(sample) == ["sample-alice"]
    assert _ids(owner) == ["dataset-bob"]


def test_owner_id_filters_collections_and_matching_legacy_aliases(client, db_engine, auth_as):
    group_id = _seed_owned_resources(db_engine)
    auth_as(ALICE, groups=[(ALICE, group_id, Role.OWNER)])

    cases = (
        ("/datasets", "owner_orcid", "dataset-bob"),
        ("/samples", "owner_orcid", "sample-bob"),
        ("/instruments", "owner", "instrument-bob"),
    )
    for path, alias, expected in cases:
        neutral = client.get(path, params={"owner_id": BOB})
        matching_alias = client.get(path, params={"owner_id": BOB, alias: BOB})
        legacy = client.get(path, params={alias: BOB})
        assert neutral.status_code == 200, (path, neutral.text)
        assert matching_alias.status_code == 200, (path, matching_alias.text)
        assert legacy.status_code == 200, (path, legacy.text)
        assert _ids(neutral) == [expected]
        assert _ids(matching_alias) == [expected]
        assert _ids(legacy) == [expected]

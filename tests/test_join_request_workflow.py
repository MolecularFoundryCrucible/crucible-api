from datetime import timedelta

import pytest
from sqlmodel import Session, select

import app.api.routes.join_requests as join_requests_routes
from app.general_utils import get_utc_datetime
from app.tables import (
    AccessGroup, AccessGroupJoinRequest, DeletionRequest, Project, Resource, ResourceACL, User, UserAccessGroupLink,
)
from app.utils.permissions import AccessGroupType, PlatformRole, Role

OWNER, EDITOR, CONTRIBUTOR = "0000-0006-0000-0001", "0000-0006-0000-0002", "0000-0006-0000-0003"
ALICE, BOB, STRANGER = "0000-0006-0000-0004", "0000-0006-0000-0005", "0000-0006-0000-0006"
ROLES = {OWNER: Role.OWNER, EDITOR: Role.EDITOR, CONTRIBUTOR: Role.CONTRIBUTOR}


@pytest.fixture
def world(db_engine):
    with Session(db_engine) as session:
        people = {}
        for orcid, first, last in ((OWNER, "Olga", "Owner"), (EDITOR, "Ed", "Itor"), (CONTRIBUTOR, "Cora", "Tributor"),
                                   (ALICE, "Alice", "Zeta"), (BOB, "Bob", "Alpha"), (STRANGER, "Stan", "Ger")):
            people[orcid] = User(first_name=first, last_name=last, email=None, unique_id=orcid, username=first.lower())
            session.add(people[orcid])
        groups = {}
        for slug, title in (("films", "Thin films"), ("bulk", "Bulk crystals")):
            project = Project(project_id=slug, title=title, organization="MF", project_lead_orcid=OWNER)
            session.add(project)
            session.commit()
            session.refresh(project)
            group = AccessGroup(group_name=project.unique_id, group_type=AccessGroupType.PROJECT.value)
            session.add(group)
            session.add(Resource(mfid=project.unique_id, resource_type="project"))
            session.commit()
            session.refresh(group)
            session.add(ResourceACL(mfid=project.unique_id, accessgroup_id=group.id, read=True, write=True, permission="owner"))
            for orcid, role in ROLES.items():
                session.add(UserAccessGroupLink(user_id=people[orcid].id, accessgroup_id=group.id, role=role.db_value))
            groups[slug] = (group.id, project.unique_id)
        session.commit()
    return groups


@pytest.fixture
def act(client, world, auth_as):
    def _as(orcid, **kwargs):
        if orcid in ROLES:
            auth_as(orcid, groups=[(mfid, group_id, ROLES[orcid]) for group_id, mfid in world.values()], **kwargs)
        else:
            auth_as(orcid, groups=[], **kwargs)
        return client
    return _as


def ask(act, orcid, slug="films", reason="please"):
    response = act(orcid).post(f"/projects/{slug}/join_requests", json={"reason": reason})
    assert response.status_code == 201, response.text
    return response.json()


def role_in(db_engine, world, orcid, slug="films"):
    with Session(db_engine) as session:
        user = session.exec(select(User).where(User.unique_id == orcid)).one()
        link = session.exec(select(UserAccessGroupLink).where(UserAccessGroupLink.user_id == user.id, UserAccessGroupLink.accessgroup_id == world[slug][0])).one_or_none()
        return link.role if link else None


def test_create_returns_the_full_request(act, world):
    body = ask(act, ALICE)
    assert body["status"] == "pending"
    assert body["project"] == {"unique_id": world["films"][1], "project_id": "films", "title": "Thin films"}
    assert body["project_mfid"] == world["films"][1]
    assert body["requester"]["first_name"] == "Alice" and "email" not in body["requester"]
    assert body["capabilities"] == {"can_review": False, "can_withdraw": True}
    assert body["group_name"] == "films"


@pytest.mark.parametrize("orcid, slug, status, code", [
    (CONTRIBUTOR, "films", 409, "already_member"),
    (ALICE, "nope", 404, "project_not_found"),
])
def test_create_refusals(act, world, orcid, slug, status, code):
    response = act(orcid).post(f"/projects/{slug}/join_requests", json={})
    assert response.status_code == status
    assert response.json()["detail"]["code"] == code


def test_one_pending_request_per_project_and_a_total_cap(act, world, monkeypatch):
    first = ask(act, ALICE)
    again = act(ALICE).post("/projects/films/join_requests", json={})
    assert again.status_code == 409
    assert again.json()["detail"] == {"code": "join_request_pending", "message": "A pending join request already exists for this project", "request_id": first["id"]}
    monkeypatch.setattr(join_requests_routes, "MAX_PENDING_PER_REQUESTER", 1)
    capped = act(ALICE).post("/projects/bulk/join_requests", json={})
    assert capped.json()["detail"]["code"] == "too_many_pending_requests"


def test_projects_pending_deletion_and_service_accounts_are_refused(act, world, db_engine):
    with Session(db_engine) as session:
        session.add(DeletionRequest(resource_id=world["bulk"][1], resource_type="project", requester_id=OWNER, status="pending"))
        session.commit()
    assert act(ALICE).post("/projects/bulk/join_requests", json={}).json()["detail"]["code"] == "project_not_accepting_requests"
    response = act(ALICE, is_service_account=True).post("/projects/films/join_requests", json={})
    assert response.status_code == 403


def test_reason_length_is_capped(act, world):
    assert act(ALICE).post("/projects/films/join_requests", json={"reason": "x" * 2001}).status_code == 422


def test_editor_approves_up_to_their_role(act, world, db_engine):
    request_id = ask(act, ALICE)["id"]
    assert act(EDITOR).patch(f"/join_requests/{request_id}", json={"status": "approved", "role": "admin"}).status_code == 403
    response = act(EDITOR).patch(f"/join_requests/{request_id}", json={"status": "approved", "role": "contributor", "reviewer_notes": "welcome"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["status"], body["granted_role"], body["reviewer"]["unique_id"], body["reviewer_notes"]) == ("approved", "contributor", EDITOR, "welcome")
    assert body["review_time"] is not None
    assert body["capabilities"] == {"can_review": False, "can_withdraw": False}
    assert role_in(db_engine, world, ALICE) == "contributor"
    closed = act(EDITOR).patch(f"/join_requests/{request_id}", json={"status": "rejected"})
    assert closed.status_code == 409
    assert closed.json()["detail"]["code"] == "join_request_closed"


def test_owner_grants_admin_and_approval_defaults_to_contributor(act, world, db_engine):
    alice = ask(act, ALICE)["id"]
    bob = ask(act, BOB)["id"]
    assert act(OWNER).patch(f"/join_requests/{alice}", json={"status": "approved", "role": "admin"}).status_code == 200
    assert act(OWNER).patch(f"/join_requests/{bob}", json={"status": "approved"}).json()["granted_role"] == "contributor"
    assert (role_in(db_engine, world, ALICE), role_in(db_engine, world, BOB)) == ("admin", "contributor")


@pytest.mark.parametrize("body", [
    {"status": "approved", "role": "owner"},
    {"status": "rejected", "role": "viewer"},
    {"status": "withdrawn"},
    {"status": "approved", "reviewer_notes": "x" * 2001},
])
def test_invalid_reviews_are_422(act, world, body):
    request_id = ask(act, ALICE)["id"]
    assert act(OWNER).patch(f"/join_requests/{request_id}", json=body).status_code == 422


def test_rejection_adds_nobody(act, world, db_engine):
    request_id = ask(act, ALICE)["id"]
    body = act(EDITOR).patch(f"/join_requests/{request_id}", json={"status": "rejected", "reviewer_notes": "not this cycle"}).json()
    assert (body["status"], body["granted_role"]) == ("rejected", None)
    assert role_in(db_engine, world, ALICE) is None
    assert ask(act, ALICE)["status"] == "pending"


def test_only_reviewers_and_the_requester_see_a_request(act, world):
    request_id = ask(act, ALICE)["id"]
    for orcid in (CONTRIBUTOR, STRANGER):
        assert act(orcid).get(f"/join_requests/{request_id}").status_code == 404
        assert act(orcid).patch(f"/join_requests/{request_id}", json={"status": "approved"}).status_code == 404
    assert act(OWNER, is_service_account=True).get(f"/join_requests/{request_id}").status_code == 404
    assert act(EDITOR).get(f"/join_requests/{request_id}").json()["capabilities"] == {"can_review": True, "can_withdraw": False}
    assert act(STRANGER, platform_role=PlatformRole.ADMIN).get(f"/join_requests/{request_id}").json()["capabilities"]["can_review"] is True
    assert act(ALICE).patch(f"/join_requests/{request_id}", json={"status": "approved"}).status_code == 403


def test_withdraw(act, world):
    request_id = ask(act, ALICE)["id"]
    assert act(EDITOR).post(f"/join_requests/{request_id}/withdraw").status_code == 403
    body = act(ALICE).post(f"/join_requests/{request_id}/withdraw").json()
    assert (body["status"], body["capabilities"]["can_withdraw"]) == ("withdrawn", False)
    assert body["review_time"] is not None
    assert act(ALICE).post(f"/join_requests/{request_id}/withdraw").json()["detail"]["code"] == "join_request_closed"
    assert act(EDITOR).patch(f"/join_requests/{request_id}", json={"status": "approved"}).status_code == 409


def test_direct_add_closes_the_pending_request(act, world):
    request_id = ask(act, ALICE)["id"]
    response = act(EDITOR).post(f"/projects/films/users/{ALICE}", params={"role": "viewer"})
    assert response.status_code == 200, response.text
    body = act(ALICE).get(f"/join_requests/{request_id}").json()
    assert (body["status"], body["granted_role"], body["reviewer_id"], body["reviewer_notes"]) == ("approved", "viewer", EDITOR, "Added to the project directly")


def ids(response):
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()["items"]]


def test_scopes(act, world):
    alice_films = ask(act, ALICE)["id"]
    bob_bulk = ask(act, BOB, slug="bulk")["id"]
    assert ids(act(ALICE).get("/join_requests")) == [alice_films]
    assert ids(act(ALICE).get("/join_requests", params={"scope": "reviewable"})) == []
    assert set(ids(act(EDITOR).get("/join_requests", params={"scope": "reviewable"}))) == {alice_films, bob_bulk}
    assert ids(act(EDITOR).get("/join_requests", params={"scope": "submitted"})) == []
    assert ids(act(CONTRIBUTOR).get("/join_requests")) == []
    assert ids(act(STRANGER, platform_role=PlatformRole.ADMIN).get("/join_requests", params={"sort": "request_time", "direction": "asc"})) == [alice_films, bob_bulk]


def test_filters(act, world, db_engine):
    alice_films = ask(act, ALICE)["id"]
    bob_films = ask(act, BOB)["id"]
    alice_bulk = ask(act, ALICE, slug="bulk")["id"]
    act(EDITOR).patch(f"/join_requests/{bob_films}", json={"status": "rejected"})
    with Session(db_engine) as session:
        old = session.get(AccessGroupJoinRequest, alice_films)
        old.request_time = get_utc_datetime() - timedelta(days=10)
        session.add(old)
        session.commit()
    editor = act(EDITOR)
    assert set(ids(editor.get("/join_requests", params={"status": "pending"}))) == {alice_films, alice_bulk}
    assert set(ids(editor.get("/join_requests", params=[("status", "pending"), ("status", "rejected")]))) == {alice_films, bob_films, alice_bulk}
    assert set(ids(editor.get("/join_requests", params={"project_mfid": world["bulk"][1]}))) == {alice_bulk}
    assert set(ids(editor.get("/join_requests", params={"group_name": "bulk"}))) == {alice_bulk}
    assert set(ids(editor.get("/join_requests", params={"requester_id": BOB}))) == {bob_films}
    assert set(ids(editor.get("/join_requests", params={"reviewer_id": EDITOR}))) == {bob_films}
    assert set(ids(editor.get("/join_requests", params={"q": "ALICE z"}))) == {alice_films, alice_bulk}
    assert set(ids(editor.get("/join_requests", params={"q": "bob"}))) == {bob_films}
    week_ago = (get_utc_datetime() - timedelta(days=7)).isoformat()
    assert set(ids(editor.get("/join_requests", params={"requested_after": week_ago}))) == {bob_films, alice_bulk}
    assert set(ids(editor.get("/join_requests", params={"requested_before": week_ago}))) == {alice_films}
    assert ids(editor.get("/join_requests", params={"reviewed_after": week_ago})) == [bob_films]
    assert set(ids(editor.get("/join_requests", params={"requested_before": week_ago.replace("+00:00", "")}))) == {alice_films}


@pytest.mark.parametrize("sort, direction, expected", [
    ("requester_name", "asc", ["Bob", "Alice", "Alice"]),
    ("project", "asc", ["bulk", "films", "films"]),
    ("status", "asc", ["pending", "pending", "rejected"]),
])
def test_sorting_pages_through_ties_with_a_cursor(act, world, sort, direction, expected):
    ask(act, ALICE)
    bob = ask(act, BOB)["id"]
    ask(act, ALICE, slug="bulk")
    act(EDITOR).patch(f"/join_requests/{bob}", json={"status": "rejected"})
    seen, cursor = [], None
    for _ in range(4):
        params = {"sort": sort, "direction": direction, "limit": 1, "scope": "reviewable"}
        if cursor:
            params["cursor"] = cursor
        page = act(EDITOR).get("/join_requests", params=params).json()
        seen += page["items"]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    key = {"requester_name": lambda item: item["requester"]["first_name"], "project": lambda item: item["project"]["project_id"], "status": lambda item: item["status"]}[sort]
    assert [key(item) for item in seen] == expected
    assert len({item["id"] for item in seen}) == 3


def test_cursor_is_bound_to_its_filters(act, world):
    ask(act, ALICE)
    ask(act, BOB)
    cursor = act(EDITOR).get("/join_requests", params={"limit": 1}).json()["next_cursor"]
    assert act(EDITOR).get("/join_requests", params={"limit": 1, "cursor": cursor, "status": "pending"}).status_code == 422


def test_facets(act, world):
    ask(act, ALICE)
    bob = ask(act, BOB)["id"]
    ask(act, ALICE, slug="bulk")
    act(EDITOR).patch(f"/join_requests/{bob}", json={"status": "rejected"})
    editor = act(EDITOR)
    status = editor.get("/join_requests/facets", params={"field": "status"}).json()
    assert [(item["value"], item["count"]) for item in status["items"]] == [("pending", 2), ("rejected", 1)]
    projects = editor.get("/join_requests/facets", params={"field": "project", "status": "pending"}).json()["items"]
    assert [(item["project"]["project_id"], item["label"], item["count"]) for item in projects] == [("bulk", "Bulk crystals", 1), ("films", "Thin films", 1)]
    requesters = editor.get("/join_requests/facets", params={"field": "requester"}).json()["items"]
    assert [(item["requester"]["unique_id"], item["label"], item["count"]) for item in requesters] == [(ALICE, "Alice Zeta", 2), (BOB, "Bob Alpha", 1)]
    assert act(ALICE).get("/join_requests/facets", params={"field": "status", "scope": "reviewable"}).json()["items"] == []


def test_deprecated_routes(act, world):
    created = act(ALICE).post("/access_groups/films/join", json={"reason": "old client"})
    assert created.status_code == 200, created.text
    assert created.json()["group_name"] == "films"
    mine = act(ALICE).get("/account/join_requests")
    assert mine.status_code == 200, mine.text
    assert [item["id"] for item in mine.json()["items"]] == [created.json()["id"]]
    assert act(ALICE).get("/account/join_requests", params={"status": "approved"}).json()["items"] == []


def test_requests_for_a_missing_project_still_list(act, world, db_engine):
    request_id = ask(act, ALICE)["id"]
    with Session(db_engine) as session:
        session.delete(session.exec(select(Project).where(Project.project_id == "films")).one())
        session.commit()
    item = act(ALICE).get("/account/join_requests").json()["items"][0]
    assert (item["id"], item["project"], item["project_mfid"], item["group_name"]) == (request_id, None, world["films"][1], world["films"][1])

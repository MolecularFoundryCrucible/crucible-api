import pytest
import sqlalchemy
from sqlmodel import Session, select

from app.models.core_models import ApiToken, Project, User
from app.models.query_params import UserListQuery
from app.utils.permissions import PlatformRole, Role

PERSON = "0000-0001-2345-6789"
OTHER = "0000-0002-3456-7890"
THIRD = "0000-0003-4567-8901"
ADMIN = "0000-0004-5678-9012"
SERVICE_ACCOUNT = "0tkn2knjast3h0008nyq9zps2c"


def _seed(db_engine):
    with Session(db_engine) as session:
        session.add_all([
            User(
                first_name="Alice",
                last_name="Researcher",
                email="alice@example.com",
                unique_id=PERSON,
                username="alice_researcher",
                platform_role=PlatformRole.NONE.value,
            ),
            User(
                first_name="Bob",
                last_name="Builder",
                email="bob@example.com",
                unique_id=OTHER,
                username="bob_builder",
                platform_role=PlatformRole.CONTRIBUTOR.value,
            ),
            User(
                first_name="Cara",
                last_name="Admin",
                email="cara@example.com",
                unique_id=ADMIN,
                username="cara_admin",
                platform_role=PlatformRole.ADMIN.value,
            ),
            User(
                first_name="Service",
                last_name="Account",
                email=None,
                unique_id=SERVICE_ACCOUNT,
                username="beamline_service",
                is_service_account=True,
                platform_role=PlatformRole.ADMIN.value,
            ),
            ApiToken(
                access_group_id=PERSON,
                user_unique_id=PERSON,
                token="alice-secret-token",
                token_created="2026-09-16",
            ),
        ])
        session.commit()


def _capabilities(response_json):
    return response_json.get("user_capabilities")


def test_self_singleton_sees_own_role_and_capabilities(client, db_engine, auth_as):
    _seed(db_engine)
    auth_as(PERSON)

    response = client.get(f"/users/{PERSON}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["platform_role"] == "none"
    assert body["email"] == "alice@example.com"
    assert _capabilities(body) == {
        "can_view_private": True,
        "can_edit_profile": True,
        "can_manage_api_key": False,
        "can_change_platform_role": False,
    }
    assert body["api_key_status"] is None


def test_admin_singleton_sees_target_role_and_key_status(client, db_engine, auth_as):
    _seed(db_engine)
    auth_as(ADMIN, platform_role=PlatformRole.ADMIN)

    response = client.get(f"/users/{PERSON}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["platform_role"] == "none"
    assert body["email"] == "alice@example.com"
    assert _capabilities(body) == {
        "can_view_private": True,
        "can_edit_profile": True,
        "can_manage_api_key": True,
        "can_change_platform_role": True,
    }
    assert body["api_key_status"] is not None
    assert body["api_key_status"]["valid"] is True
    assert body["api_key_status"]["created_at"] == "2026-09-16"


def test_admin_service_account_singleton_gets_no_role_change_flag(client, db_engine, auth_as):
    _seed(db_engine)
    auth_as(SERVICE_ACCOUNT, platform_role=PlatformRole.ADMIN, is_service_account=True)

    response = client.get(f"/users/{PERSON}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["email"] == "alice@example.com"
    assert _capabilities(body) == {
        "can_view_private": True,
        "can_edit_profile": True,
        "can_manage_api_key": True,
        "can_change_platform_role": False,
    }
    assert body["api_key_status"] is not None


def test_ordinary_user_sees_public_shape_without_private_fields(client, db_engine, auth_as):
    _seed(db_engine)
    auth_as(OTHER)

    response = client.get(f"/users/{PERSON}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert "email" not in body
    assert "platform_role" not in body
    assert _capabilities(body) == {
        "can_view_private": False,
        "can_edit_profile": False,
        "can_manage_api_key": False,
        "can_change_platform_role": False,
    }
    assert "api_key_status" not in body


def test_by_username_lookup_populates_capabilities_for_self(client, db_engine, auth_as):
    _seed(db_engine)
    auth_as(PERSON)

    response = client.get("/users/by-username/alice_researcher")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["platform_role"] == "none"
    assert _capabilities(body)["can_view_private"] is True


def test_exact_username_query_populates_capabilities_for_admin(client, db_engine, auth_as):
    _seed(db_engine)
    auth_as(ADMIN, platform_role=PlatformRole.ADMIN)

    response = client.get("/users", params={"username": "alice_researcher"})

    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["platform_role"] == "none"
    assert item["email"] == "alice@example.com"
    assert _capabilities(item)["can_manage_api_key"] is True


def test_exact_unique_id_query_populates_capabilities_for_self(client, db_engine, auth_as):
    _seed(db_engine)
    auth_as(PERSON)

    response = client.get("/users", params={"unique_id": PERSON})

    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["email"] == "alice@example.com"
    assert _capabilities(item)["can_view_private"] is True


def test_exact_email_query_populates_capabilities_for_self(client, db_engine, auth_as):
    _seed(db_engine)
    auth_as(PERSON)

    response = client.get("/users", params={"email": "alice@example.com"})

    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert _capabilities(item)["can_view_private"] is True


def test_admin_service_account_target_gets_no_role_change_flag(client, db_engine, auth_as):
    _seed(db_engine)
    auth_as(ADMIN, platform_role=PlatformRole.ADMIN)

    response = client.get(f"/users/{SERVICE_ACCOUNT}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["is_service_account"] is True
    assert _capabilities(body)["can_change_platform_role"] is False


def test_capabilities_are_null_on_broad_listing(client, db_engine, auth_as):
    _seed(db_engine)
    auth_as(ADMIN, platform_role=PlatformRole.ADMIN)

    response = client.get("/users")

    assert response.status_code == 200, response.text
    for item in response.json()["items"]:
        assert _capabilities(item) is None
        assert item["platform_role"] in {"none", "contributor", "admin"}


def test_capabilities_are_null_on_search_and_resolve(client, db_engine, auth_as, monkeypatch):
    _seed(db_engine)
    auth_as(ADMIN, platform_role=PlatformRole.ADMIN)
    monkeypatch.setattr("app.api.routes.users.configure_trigram_search", lambda session, threshold: None)
    monkeypatch.setattr("app.api.routes.users.apply_trigram_search", lambda query, tokens, fields, prefix_fields=(): query)
    monkeypatch.setattr(
        "app.api.routes.users.trigram_rank",
        lambda query, fields: sqlalchemy.literal(1),
    )

    search = client.get("/users/search", params={"q": "alice"})
    assert search.status_code == 200, search.text
    for item in search.json():
        assert _capabilities(item) is None
        assert "email" not in item

    resolve = client.post("/users/resolve", json={"usernames": ["alice_researcher"]})
    assert resolve.status_code == 200, resolve.text
    for item in resolve.json().values():
        assert _capabilities(item) is None


def test_account_routes_leave_user_capabilities_null(client, db_engine, auth_as):
    _seed(db_engine)
    auth_as(PERSON)

    info = client.get("/account")
    assert info.status_code == 200, info.text
    assert _capabilities(info.json()["user_info"]) is None
    assert info.json()["user_info"]["platform_role"] == "none"


def test_no_capabilities_key_on_any_user_response(client, db_engine, auth_as, monkeypatch):
    _seed(db_engine)
    auth_as(ADMIN, platform_role=PlatformRole.ADMIN)
    monkeypatch.setattr("app.api.routes.users.configure_trigram_search", lambda session, threshold: None)
    monkeypatch.setattr("app.api.routes.users.apply_trigram_search", lambda query, tokens, fields, prefix_fields=(): query)
    monkeypatch.setattr(
        "app.api.routes.users.trigram_rank",
        lambda query, fields: sqlalchemy.literal(1),
    )

    responses = (
        client.get("/users"),
        client.get(f"/users/{PERSON}"),
        client.get("/users/by-username/alice_researcher"),
        client.get("/users/search", params={"q": "alice"}),
        client.post("/users/resolve", json={"usernames": ["alice_researcher"]}),
        client.get("/account"),
    )
    for response in responses:
        assert response.status_code == 200, response.text
        body = response.json()
        payloads = []
        if isinstance(body, list):
            payloads = body
        elif "items" in body:
            payloads = body["items"]
        elif "user_info" in body and body["user_info"]:
            payloads = [body["user_info"]]
        elif "user_unique_id" in body:
            payloads = []
        elif "unique_id" in body:
            payloads = [body]
        else:
            payloads = list(body.values())
        for payload in payloads:
            if isinstance(payload, dict):
                assert "capabilities" not in payload


def test_admin_directory_exposes_private_fields_non_admin_does_not(client, db_engine, auth_as):
    _seed(db_engine)
    auth_as(ADMIN, platform_role=PlatformRole.ADMIN)
    admin_listing = client.get("/users")
    assert admin_listing.status_code == 200, admin_listing.text
    admin_items = {item["unique_id"]: item for item in admin_listing.json()["items"]}
    assert admin_items[PERSON]["email"] == "alice@example.com"
    assert admin_items[OTHER]["email"] == "bob@example.com"

    auth_as(THIRD)
    other_listing = client.get("/users")
    assert other_listing.status_code == 200, other_listing.text
    for item in other_listing.json()["items"]:
        assert "email" not in item


def test_platform_role_filter_admin_ok(client, db_engine, auth_as):
    _seed(db_engine)
    auth_as(ADMIN, platform_role=PlatformRole.ADMIN)

    response = client.get("/users", params=[("platform_role", "admin"), ("platform_role", "contributor")])

    assert response.status_code == 200, response.text
    roles = {item["unique_id"]: item["platform_role"] for item in response.json()["items"]}
    assert roles[ADMIN] == "admin"
    assert roles[SERVICE_ACCOUNT] == "admin"
    assert roles[OTHER] == "contributor"
    assert PERSON not in roles


def test_platform_role_filter_non_admin_403(client, db_engine, auth_as):
    _seed(db_engine)
    auth_as(PERSON)

    response = client.get("/users", params={"platform_role": "admin"})

    assert response.status_code == 403, response.text


def test_sort_by_name_both_directions(client, db_engine, auth_as):
    with Session(db_engine) as session:
        session.add(User(
            first_name="Zara",
            last_name="aaa",
            unique_id="0000-0009-9999-9991",
            username="zara_user",
        ))
        session.commit()
    _seed(db_engine)
    auth_as(ADMIN, platform_role=PlatformRole.ADMIN)

    ascending = client.get("/users", params={"sort": "name", "direction": "asc"})
    assert ascending.status_code == 200, ascending.text
    names = [item["last_name"].lower() for item in ascending.json()["items"]]
    assert names == sorted(names)

    descending = client.get("/users", params={"sort": "name", "direction": "desc"})
    assert descending.status_code == 200, descending.text
    names = [item["last_name"].lower() for item in descending.json()["items"]]
    assert names == sorted(names, reverse=True)


def test_sort_by_username_both_directions(client, db_engine, auth_as):
    _seed(db_engine)
    auth_as(ADMIN, platform_role=PlatformRole.ADMIN)

    ascending = client.get("/users", params={"sort": "username", "direction": "asc"})
    assert ascending.status_code == 200, ascending.text
    usernames = [item["username"] for item in ascending.json()["items"]]
    assert usernames == sorted(usernames, key=str.lower)

    descending = client.get("/users", params={"sort": "username", "direction": "desc"})
    assert descending.status_code == 200, descending.text
    usernames = [item["username"] for item in descending.json()["items"]]
    assert usernames == sorted(usernames, key=str.lower, reverse=True)


def test_default_order_unchanged_without_sort(client, db_engine, auth_as):
    _seed(db_engine)
    auth_as(ADMIN, platform_role=PlatformRole.ADMIN)

    response = client.get("/users")
    assert response.status_code == 200, response.text
    last_names = [item["last_name"] for item in response.json()["items"]]
    assert last_names == sorted(last_names)


def test_include_total_controls_count(client, db_engine, auth_as):
    _seed(db_engine)
    auth_as(ADMIN, platform_role=PlatformRole.ADMIN)

    default_page = client.get("/users")
    assert default_page.json()["total"] == 4

    suppressed = client.get("/users", params={"include_total": "false"})
    assert suppressed.json()["total"] is None

    requested = client.get("/users", params={"include_total": "true", "limit": 1})
    assert requested.json()["total"] == 4


def test_cursor_pagination_continues_and_binds_context(client, db_engine, auth_as):
    _seed(db_engine)
    auth_as(ADMIN, platform_role=PlatformRole.ADMIN)
    params = {"sort": "username", "direction": "asc", "limit": 2}

    first = client.get("/users", params=params)
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["total"] == 4
    assert first_body["next_cursor"].startswith("c1.")
    assert len(first_body["items"]) == 2

    second = client.get("/users", params={**params, "cursor": first_body["next_cursor"]})
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert second_body["total"] == 4
    assert len(second_body["items"]) == 2

    all_usernames = [item["username"] for item in first_body["items"] + second_body["items"]]
    assert all_usernames == sorted(all_usernames, key=str.lower)

    changed = client.get("/users", params={
        **params, "q": "alice", "cursor": first_body["next_cursor"],
    })
    assert changed.status_code == 422

    auth_as(PERSON)
    other_user = client.get("/users", params={**params, "cursor": first_body["next_cursor"]})
    assert other_user.status_code == 422


def test_cursor_takes_precedence_over_offset(client, db_engine, auth_as):
    _seed(db_engine)
    auth_as(ADMIN, platform_role=PlatformRole.ADMIN)

    first = client.get("/users", params={"limit": 1})
    continuation = client.get("/users", params={
        "limit": 1, "cursor": first.json()["next_cursor"], "offset": 999999,
    })
    assert continuation.status_code == 200, continuation.text
    assert continuation.json()["offset"] is None
    assert len(continuation.json()["items"]) == 1


def test_q_filter_matches_name_fields_and_username(client, db_engine, auth_as):
    _seed(db_engine)
    auth_as(ADMIN, platform_role=PlatformRole.ADMIN)

    by_name = client.get("/users", params={"q": "alice"})
    assert [item["unique_id"] for item in by_name.json()["items"]] == [PERSON]

    by_username = client.get("/users", params={"q": "builder"})
    assert [item["unique_id"] for item in by_username.json()["items"]] == [OTHER]

    by_email_name = client.get("/users", params={"q": "cara"})
    assert [item["unique_id"] for item in by_email_name.json()["items"]] == [ADMIN]


def test_first_name_and_last_name_filters_permissive_and_exact(client, db_engine, auth_as):
    _seed(db_engine)
    auth_as(ADMIN, platform_role=PlatformRole.ADMIN)

    permissive = client.get("/users", params={"first_name": "lic"})
    assert [item["unique_id"] for item in permissive.json()["items"]] == [PERSON]

    exact = client.get("/users", params={"first_name": "Alice", "permissive": "false"})
    assert [item["unique_id"] for item in exact.json()["items"]] == [PERSON]

    partial_exact = client.get("/users", params={"first_name": "lic", "permissive": "false"})
    assert partial_exact.json()["items"] == []

    permissive_last = client.get("/users", params={"last_name": "build"})
    assert [item["unique_id"] for item in permissive_last.json()["items"]] == [OTHER]


def test_undeclared_param_is_ignored_with_deprecation_header(client, db_engine, auth_as):
    _seed(db_engine)
    auth_as(ADMIN, platform_role=PlatformRole.ADMIN)

    response = client.get("/users", params={"projects": "x", "id": "1"})
    assert response.status_code == 200, response.text
    assert response.headers["Deprecation"].startswith("@")
    assert response.json()["total"] == 4


def test_role_route_promote_and_demote(client, db_engine, auth_as):
    _seed(db_engine)
    auth_as(ADMIN, platform_role=PlatformRole.ADMIN)

    promote = client.patch(f"/users/{PERSON}/platform_role", json={"platform_role": "admin"})
    assert promote.status_code == 200, promote.text
    body = promote.json()
    assert body["platform_role"] == "admin"
    assert _capabilities(body)["can_view_private"] is True

    demote = client.patch(f"/users/{PERSON}/platform_role", json={"platform_role": "none"})
    assert demote.status_code == 200, demote.text
    assert demote.json()["platform_role"] == "none"

    with Session(db_engine) as session:
        user = session.exec(select(User).where(User.unique_id == PERSON)).one()
        assert user.platform_role == "none"


def test_role_route_rejects_self_change(client, db_engine, auth_as):
    _seed(db_engine)
    auth_as(ADMIN, platform_role=PlatformRole.ADMIN)

    response = client.patch(f"/users/{ADMIN}/platform_role", json={"platform_role": "none"})
    assert response.status_code == 403, response.text


def test_role_route_rejects_service_account_caller(client, db_engine, auth_as):
    _seed(db_engine)
    auth_as(SERVICE_ACCOUNT, platform_role=PlatformRole.ADMIN, is_service_account=True)

    response = client.patch(f"/users/{PERSON}/platform_role", json={"platform_role": "admin"})
    assert response.status_code == 403, response.text


def test_role_route_rejects_non_admin(client, db_engine, auth_as):
    _seed(db_engine)
    auth_as(PERSON)

    response = client.patch(f"/users/{OTHER}/platform_role", json={"platform_role": "admin"})
    assert response.status_code == 403, response.text


def test_role_route_rejects_service_account_target(client, db_engine, auth_as):
    _seed(db_engine)
    auth_as(ADMIN, platform_role=PlatformRole.ADMIN)

    response = client.patch(f"/users/{SERVICE_ACCOUNT}/platform_role", json={"platform_role": "none"})
    assert response.status_code == 409, response.text
    assert "service_accounts" in response.json()["detail"]


def test_role_route_rejects_demoting_last_human_admin(client, db_engine, auth_as):
    with Session(db_engine) as session:
        session.add_all([
            User(
                first_name="Lone",
                last_name="Admin",
                unique_id="0000-0007-7777-7771",
                username="lone_admin",
                platform_role=PlatformRole.ADMIN.value,
            ),
            User(
                first_name="Stale",
                last_name="Admin",
                unique_id="0000-0007-7777-7773",
                username="caller_admin",
                platform_role=PlatformRole.NONE.value,
            ),
        ])
        session.commit()
    # The caller's principal still carries admin from before their row was
    # demoted; the auth cache keeps it valid for up to AUTH_CACHE_TTL.
    auth_as("0000-0007-7777-7773", platform_role=PlatformRole.ADMIN)

    demote_last = client.patch(
        "/users/0000-0007-7777-7771/platform_role", json={"platform_role": "none"})
    assert demote_last.status_code == 409, demote_last.text
    assert "last human platform administrator" in demote_last.json()["detail"]


def test_role_route_allows_demoting_when_other_human_admins_remain(client, db_engine, auth_as):
    with Session(db_engine) as session:
        session.add_all([
            User(
                first_name="Lone",
                last_name="Admin",
                unique_id="0000-0007-7777-7781",
                username="lone_admin_two",
                platform_role=PlatformRole.ADMIN.value,
            ),
            User(
                first_name="Active",
                last_name="Admin",
                unique_id="0000-0007-7777-7783",
                username="caller_admin_two",
                platform_role=PlatformRole.ADMIN.value,
            ),
        ])
        session.commit()
    auth_as("0000-0007-7777-7783", platform_role=PlatformRole.ADMIN)

    response = client.patch(
        "/users/0000-0007-7777-7781/platform_role", json={"platform_role": "none"})
    assert response.status_code == 200, response.text
    assert response.json()["platform_role"] == "none"


def test_role_route_rejects_invalid_role(client, db_engine, auth_as):
    _seed(db_engine)
    auth_as(ADMIN, platform_role=PlatformRole.ADMIN)

    response = client.patch(f"/users/{PERSON}/platform_role", json={"platform_role": "support"})
    assert response.status_code == 422, response.text

    extra = client.patch(f"/users/{PERSON}/platform_role", json={
        "platform_role": "admin", "support": True,
    })
    assert extra.status_code == 422, extra.text


def test_role_route_unknown_user_404(client, db_engine, auth_as):
    _seed(db_engine)
    auth_as(ADMIN, platform_role=PlatformRole.ADMIN)

    response = client.patch("/users/missing_user/platform_role", json={"platform_role": "admin"})
    assert response.status_code == 404, response.text


def test_role_route_resolves_username_and_email(client, db_engine, auth_as):
    _seed(db_engine)
    auth_as(ADMIN, platform_role=PlatformRole.ADMIN)

    by_username = client.patch("/users/bob_builder/platform_role", json={"platform_role": "none"})
    assert by_username.status_code == 200, by_username.text

    by_email = client.patch("/users/bob@example.com/platform_role", json={"platform_role": "none"})
    assert by_email.status_code == 200, by_email.text


def test_delete_apikey_admin_only(client, db_engine, auth_as):
    _seed(db_engine)
    auth_as(PERSON)

    response = client.delete(f"/users/{PERSON}/apikey")
    assert response.status_code == 403, response.text


def test_delete_apikey_unknown_user_404(client, db_engine, auth_as):
    _seed(db_engine)
    auth_as(ADMIN, platform_role=PlatformRole.ADMIN)

    response = client.delete("/users/missing_user/apikey")
    assert response.status_code == 404, response.text


def test_delete_apikey_revokes_tokens(client, db_engine, auth_as):
    _seed(db_engine)
    auth_as(ADMIN, platform_role=PlatformRole.ADMIN)

    response = client.delete(f"/users/{PERSON}/apikey")
    assert response.status_code == 204, response.text

    with Session(db_engine) as session:
        remaining = session.exec(
            select(ApiToken).where(ApiToken.user_unique_id == PERSON)
        ).all()
        assert remaining == []


def test_delete_apikey_works_for_service_accounts(client, db_engine, auth_as):
    _seed(db_engine)
    auth_as(ADMIN, platform_role=PlatformRole.ADMIN)

    response = client.delete(f"/users/{SERVICE_ACCOUNT}/apikey")
    assert response.status_code == 204, response.text


def test_api_key_status_present_for_admin_singleton_only(client, db_engine, auth_as):
    _seed(db_engine)
    auth_as(ADMIN, platform_role=PlatformRole.ADMIN)

    singleton = client.get(f"/users/{PERSON}")
    assert singleton.json()["api_key_status"] is not None

    exact = client.get("/users", params={"username": "alice_researcher"})
    assert exact.json()["items"][0]["api_key_status"] is not None

    listing = client.get("/users")
    for item in listing.json()["items"]:
        assert item["api_key_status"] is None

    auth_as(PERSON)
    self_view = client.get(f"/users/{PERSON}")
    assert self_view.json()["api_key_status"] is None


def test_per_project_role_on_user_projects_route(client, db_engine, auth_as):
    _seed(db_engine)
    with Session(db_engine) as session:
        target = session.exec(select(User).where(User.unique_id == PERSON)).one()
        proj = Project(
            project_id="proj_role", organization="LBNL",
            project_lead_orcid=PERSON, title="Role Project",
        )
        session.add(proj)
        session.commit()
        session.refresh(proj)
        from app.models.core_models import AccessGroup, UserAccessGroupLink
        group = session.exec(select(AccessGroup).where(
            AccessGroup.group_name == proj.unique_id)).one_or_none()
        if group is None:
            group = AccessGroup(group_name=proj.unique_id)
            session.add(group)
            session.commit()
            session.refresh(group)
        session.add(UserAccessGroupLink(
            user_id=target.id, accessgroup_id=group.id, role="editor"))
        session.commit()

    auth_as(PERSON, groups=[])
    response = client.get(f"/users/{PERSON}/projects")
    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["project_id"] == "proj_role"
    assert item["role"] == "editor"


def test_can_manage_users_in_account_capabilities(client, db_engine, auth_as):
    _seed(db_engine)
    auth_as(ADMIN, platform_role=PlatformRole.ADMIN)
    admin_profile = client.get("/account/profile")
    assert admin_profile.json()["capabilities"]["can_manage_users"] is True

    auth_as(PERSON)
    person_profile = client.get("/account/profile")
    assert person_profile.json()["capabilities"]["can_manage_users"] is False


def test_account_profile_capabilities_still_validate(client, db_engine, auth_as):
    _seed(db_engine)
    auth_as(PERSON)

    response = client.get("/account/profile")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["capabilities"]["can_manage_users"] is False
    assert body["authorization"]["platform_role"] == "none"
    assert body["platform_role"] == "none"


def test_user_list_query_model_defaults():
    params = UserListQuery()
    assert params.limit == 100
    assert params.offset == 0
    assert params.sort is None
    assert params.include_total is None


PAGINATION_USERS = [
    ("0000-0009-0000-0001", "Ann", "Smith", "ann", "ann@x.com"),
    ("0000-0009-0000-0002", "bob", "smith", None, "Bob@x.com"),
    ("0000-0009-0000-0003", "Ann", "Smith", "zed", None),
    ("0000-0009-0000-0004", "Dee", "Jones", "Dee", "dee@x.com"),
    ("0000-0009-0000-0005", "eve", "SMITH", None, None),
    ("0000-0009-0000-0006", "Fay", "Adams", "fay", "fay@x.com"),
    ("0000-0009-0000-0007", "Gus", "smith", "ann", "gus@x.com"),
]


def _seed_pagination_users(db_engine):
    with Session(db_engine) as session:
        for unique_id, first, last, username, email in PAGINATION_USERS:
            session.add(User(unique_id=unique_id, first_name=first, last_name=last,
                             username=username, email=email))
        session.commit()


def _expected_order(sort, direction):
    def key(row):
        unique_id, first, last, username, email = row
        value = {
            "name": f"{last.lower()} {first.lower()}",
            "username": username.lower() if username else None,
            "email": email.lower() if email else None,
            "unique_id": unique_id.lower(),
        }[sort]
        return value, unique_id

    present = sorted((r for r in PAGINATION_USERS if key(r)[0] is not None), key=key,
                     reverse=direction == "desc")
    missing = sorted((r for r in PAGINATION_USERS if key(r)[0] is None), key=lambda r: r[0],
                     reverse=direction == "desc")
    return [row[0] for row in present + missing]


@pytest.mark.parametrize("sort", ["name", "username", "email", "unique_id"])
@pytest.mark.parametrize("direction", ["asc", "desc"])
@pytest.mark.parametrize("limit", [1, 2, 3])
def test_cursor_walk_visits_every_user_once_in_order(client, db_engine, auth_as, sort, direction, limit):
    _seed_pagination_users(db_engine)
    auth_as("admin-walk", groups=[], platform_role=PlatformRole.ADMIN)

    seen, cursor = [], None
    for _ in range(len(PAGINATION_USERS) + 2):
        params = {"sort": sort, "direction": direction, "limit": limit, "q": "0000-0009"}
        if cursor:
            params["cursor"] = cursor
        body = client.get("/users", params=params).json()
        seen += [item["unique_id"] for item in body["items"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert seen == _expected_order(sort, direction)


def test_default_order_cursor_walk_matches_name_order(client, db_engine, auth_as):
    _seed_pagination_users(db_engine)
    auth_as("admin-walk", groups=[], platform_role=PlatformRole.ADMIN)

    seen, cursor = [], None
    for _ in range(len(PAGINATION_USERS) + 2):
        params = {"limit": 2, "q": "0000-0009"}
        if cursor:
            params["cursor"] = cursor
        body = client.get("/users", params=params).json()
        seen += [item["unique_id"] for item in body["items"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert seen == _expected_order("name", "asc")


def test_email_sort_and_search_field_are_admin_only(client, db_engine, auth_as):
    _seed_pagination_users(db_engine)
    auth_as("0000-0009-0000-0001", groups=[])

    assert client.get("/users", params={"sort": "email"}).status_code == 403
    assert client.get("/users", params={"q": "x.com", "search_fields": "email"}).status_code == 403


def test_q_default_fields_exclude_email_for_non_admins(client, db_engine, auth_as):
    _seed_pagination_users(db_engine)
    auth_as("admin-walk", groups=[], platform_role=PlatformRole.ADMIN)
    admin_hits = {u["unique_id"] for u in client.get("/users", params={"q": "gus@"}).json()["items"]}
    assert admin_hits == {"0000-0009-0000-0007"}

    auth_as("0000-0009-0000-0001", groups=[])
    assert client.get("/users", params={"q": "gus@"}).json()["items"] == []


def test_search_fields_restricts_search_to_named_fields(client, db_engine, auth_as):
    _seed_pagination_users(db_engine)
    auth_as("admin-walk", groups=[], platform_role=PlatformRole.ADMIN)

    by_username = client.get("/users", params={"q": "ann", "search_fields": "username"}).json()["items"]
    assert {u["unique_id"] for u in by_username} == {"0000-0009-0000-0001", "0000-0009-0000-0007"}

    by_id = client.get("/users", params={"q": "0000-0009-0000-000", "search_fields": "unique_id"}).json()["items"]
    assert len(by_id) == len(PAGINATION_USERS)

    by_email = client.get("/users", params=[("q", "x.com"), ("search_fields", "email")]).json()["items"]
    assert {u["unique_id"] for u in by_email} == {
        "0000-0009-0000-0001", "0000-0009-0000-0002", "0000-0009-0000-0004",
        "0000-0009-0000-0006", "0000-0009-0000-0007",
    }


def test_search_fields_requires_q(client, db_engine, auth_as):
    auth_as("admin-walk", groups=[], platform_role=PlatformRole.ADMIN)
    assert client.get("/users", params={"search_fields": "username"}).status_code == 422


def test_removed_routes_are_gone(client, db_engine, auth_as):
    _seed(db_engine)
    auth_as(PERSON)
    assert client.get("/users/me").status_code == 404
    assert client.patch("/users/me", json={"first_name": "X"}).status_code in (403, 404, 405)
    assert client.get("/samples/any/datasets").status_code == 404

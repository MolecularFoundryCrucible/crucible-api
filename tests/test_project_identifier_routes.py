from sqlmodel import Session, select

from app.tables import AccessGroup, Dataset, Project, Resource, ResourceACL, User, UserAccessGroupLink
from app.utils.permissions import AccessGroupType, PlatformRole, Role


def _setup(db_engine):
    with Session(db_engine) as session:
        lead = User(
            first_name="Project",
            last_name="Lead",
            email="lead@example.com",
            unique_id="0000-0001-1111-1111",
            username="project_lead",
        )
        outsider = User(
            first_name="Project",
            last_name="Outsider",
            email="outsider@example.com",
            unique_id="0000-0002-2222-2222",
            username="project_outsider",
        )
        session.add(lead)
        session.add(outsider)
        session.flush()
        project = Project(
            project_id="project-slug",
            organization="LBNL",
            project_lead_orcid=lead.unique_id,
            lead_user_id=lead.id,
        )
        session.add(project)
        session.commit()
        session.refresh(project)
        session.refresh(lead)

        group = AccessGroup(
            group_name=project.unique_id,
            group_type=AccessGroupType.PROJECT.value,
        )
        session.add(group)
        session.add(Resource(mfid=project.unique_id, resource_type="project"))
        session.commit()
        session.refresh(group)
        session.add(UserAccessGroupLink(user_id=lead.id, accessgroup_id=group.id, role="owner"))
        session.add(ResourceACL(
            mfid=project.unique_id,
            accessgroup_id=group.id,
            read=True,
            write=True,
            permission="owner",
        ))
        session.commit()
        return project.unique_id, group.id


def test_canonical_get_uses_project_mfid(client, db_engine, auth_as):
    project_mfid, _ = _setup(db_engine)
    auth_as("0000-0002-2222-2222", groups=[], platform_role=PlatformRole.NONE)

    response = client.get(f"/projects/{project_mfid}")

    assert response.status_code == 200, response.text
    assert response.json()["unique_id"] == project_mfid
    assert "content-location" not in response.headers
    assert response.json()["capabilities"] == {
        "can_edit": False,
        "can_manage_access": False,
        "can_view_access": False,
        "can_change_status": False,
        "can_transfer": False,
        "can_delete": False,
        "can_delete_files": False,
        "can_request_deletion": False,
        "max_grant_role": None,
    }
    generic_response = client.get(f"/resources/{project_mfid}")
    assert generic_response.status_code == 200, generic_response.text
    assert generic_response.json() == response.json()


def test_legacy_slug_get_returns_canonical_location(client, db_engine, auth_as):
    project_mfid, _ = _setup(db_engine)
    auth_as("0000-0002-2222-2222", groups=[], platform_role=PlatformRole.NONE)

    response = client.get("/projects/project-slug")

    assert response.status_code == 200, response.text
    assert response.json()["unique_id"] == project_mfid
    assert response.headers["content-location"] == f"/projects/{project_mfid}"


def test_exact_slug_query_returns_one_public_safe_project(client, db_engine, auth_as):
    project_mfid, _ = _setup(db_engine)
    auth_as("0000-0002-2222-2222", groups=[], platform_role=PlatformRole.NONE)

    response = client.get("/projects", params={"project_id": "project-slug"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1
    assert body["items"][0]["unique_id"] == project_mfid
    assert "email" not in body["items"][0]["lead"]
    assert body["items"][0]["capabilities"]["can_edit"] is False


def test_exact_slug_query_returns_empty_collection_when_missing(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as("0000-0002-2222-2222", groups=[], platform_role=PlatformRole.NONE)

    response = client.get("/projects", params={"project_id": "missing-project"})

    assert response.status_code == 200, response.text
    assert response.json()["total"] == 0
    assert response.json()["items"] == []


def test_exact_slug_query_keeps_lead_email_private_for_members(client, db_engine, auth_as):
    project_mfid, group_id = _setup(db_engine)
    auth_as("0000-0001-1111-1111", groups=[(project_mfid, group_id, Role.OWNER)])

    response = client.get("/projects", params={"project_id": "project-slug"})

    assert response.status_code == 200, response.text
    assert "email" not in response.json()["items"][0]["lead"]


def test_project_update_accepts_canonical_mfid(client, db_engine, auth_as):
    project_mfid, group_id = _setup(db_engine)
    auth_as("0000-0001-1111-1111", groups=[(project_mfid, group_id, Role.EDITOR)])

    response = client.patch(f"/projects/{project_mfid}", json={"title": "Updated title"})

    assert response.status_code == 200, response.text
    assert response.json()["unique_id"] == project_mfid
    assert response.json()["title"] == "Updated title"
    assert response.json()["capabilities"] == {
        "can_edit": True,
        "can_manage_access": True,
        "can_view_access": True,
        "can_change_status": True,
        "can_transfer": False,
        "can_delete": False,
        "can_delete_files": False,
        "can_request_deletion": False,
        "max_grant_role": "editor",
    }
    generic_response = client.get(f"/resources/{project_mfid}")
    assert generic_response.status_code == 200, generic_response.text
    assert generic_response.json()["capabilities"] == response.json()["capabilities"]


def test_project_membership_read_accepts_canonical_mfid(client, db_engine, auth_as):
    project_mfid, group_id = _setup(db_engine)
    auth_as("0000-0001-1111-1111", groups=[(project_mfid, group_id, Role.OWNER)])

    response = client.get(f"/projects/{project_mfid}/users")

    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["unique_id"] == "0000-0001-1111-1111"
    assert "email" not in response.json()["items"][0]


def test_project_membership_mutations_accept_canonical_mfid(client, db_engine, auth_as):
    project_mfid, group_id = _setup(db_engine)
    auth_as("0000-0001-1111-1111", groups=[(project_mfid, group_id, Role.OWNER)])
    member_id = "0000-0002-2222-2222"

    added = client.post(f"/projects/{project_mfid}/users/{member_id}")
    assert added.status_code == 200, added.text
    assert any(item["unique_id"] == member_id for item in added.json())

    repeated = client.post(f"/projects/{project_mfid}/users/{member_id}")
    assert repeated.status_code == 200, repeated.text
    assert next(item for item in repeated.json() if item["unique_id"] == member_id)["role"] == "contributor"

    conflicting = client.post(
        f"/projects/{project_mfid}/users/{member_id}",
        params={"role": Role.VIEWER.db_value},
    )
    assert conflicting.status_code == 409, conflicting.text
    assert "Use PATCH" in conflicting.json()["detail"]

    updated = client.patch(
        f"/projects/{project_mfid}/users/{member_id}",
        params={"role": Role.VIEWER.db_value},
    )
    assert updated.status_code == 200, updated.text
    assert next(item for item in updated.json() if item["unique_id"] == member_id)["role"] == "viewer"

    removed = client.delete(f"/projects/{project_mfid}/users/{member_id}")
    assert removed.status_code == 200, removed.text
    assert all(item["unique_id"] != member_id for item in removed.json())


def test_service_account_cannot_manage_members_through_owner_standing(client, db_engine, auth_as):
    project_mfid, group_id = _setup(db_engine)
    member_id = "0000-0002-2222-2222"
    with Session(db_engine) as session:
        member = session.exec(select(User).where(User.unique_id == member_id)).one()
        session.add(UserAccessGroupLink(
            user_id=member.id,
            accessgroup_id=group_id,
            role=Role.CONTRIBUTOR.db_value,
        ))
        session.commit()
    auth_as(
        "0000-0001-1111-1111",
        groups=[(project_mfid, group_id, Role.OWNER)],
        is_service_account=True,
    )

    response = client.delete(f"/projects/{project_mfid}/users/{member_id}")

    assert response.status_code == 403


def test_project_graphs_accept_mfid_and_slug(client, db_engine, auth_as):
    project_mfid, group_id = _setup(db_engine)
    auth_as("0000-0001-1111-1111", groups=[(project_mfid, group_id, Role.OWNER)])

    canonical = client.get(f"/project_graph/{project_mfid}")
    compatibility = client.get("/project_graph/project-slug")

    assert canonical.status_code == 200, canonical.text
    assert compatibility.status_code == 200, compatibility.text
    assert canonical.json() == compatibility.json()


def test_missing_project_graph_returns_404(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as("0000-0002-2222-2222")

    response = client.get("/project_graph/missing-project")

    assert response.status_code == 404


def test_dataset_graph_ignores_legacy_public_boolean_without_acl(client, db_engine, auth_as):
    project_mfid, _ = _setup(db_engine)
    with Session(db_engine) as session:
        session.add(Dataset(
            unique_id="ds_stale_public",
            dataset_name="stale public",
            public=True,
            project_mfid=project_mfid,
        ))
        session.add(Resource(mfid="ds_stale_public", resource_type="dataset", public=True))
        session.commit()
    auth_as("0000-0002-2222-2222")

    response = client.get(f"/project_graph/{project_mfid}")

    assert response.status_code == 200, response.text
    assert response.json()["nodes"] == []


def test_platform_admin_dataset_graph_does_not_require_group_membership(client, db_engine, auth_as):
    project_mfid, _ = _setup(db_engine)
    with Session(db_engine) as session:
        session.add(Dataset(
            unique_id="ds_private_admin",
            dataset_name="private",
            public=False,
            project_mfid=project_mfid,
        ))
        session.add(Resource(mfid="ds_private_admin", resource_type="dataset", public=False))
        session.commit()
    auth_as("platform-admin", platform_role=PlatformRole.ADMIN)

    response = client.get(f"/project_graph/{project_mfid}")

    assert response.status_code == 200, response.text
    assert [node["id"] for node in response.json()["nodes"]] == ["ds_private_admin"]


def test_slug_lookups_ignore_case(client, db_engine, auth_as):
    project_mfid, _ = _setup(db_engine)
    auth_as("0000-0002-2222-2222", groups=[], platform_role=PlatformRole.NONE)

    items = client.get("/projects", params={"project_id": "PROJECT-Slug"}).json()["items"]
    assert [item["unique_id"] for item in items] == [project_mfid]

    response = client.get("/projects/Project-Slug")
    assert response.status_code == 200, response.text
    assert response.json()["project_id"] == "project-slug"
    assert response.headers["content-location"] == f"/projects/{project_mfid}"


def test_create_rejects_a_slug_differing_only_by_case(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as("0000-0001-1111-1111", groups=[], platform_role=PlatformRole.ADMIN)

    response = client.post("/projects", json={
        "project_id": "Project-Slug",
        "organization": "LBNL",
        "project_lead": "0000-0001-1111-1111",
    })
    assert response.status_code == 409

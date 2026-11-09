from sqlmodel import Session, select

from app.api.routes import users as users_routes
from app.models.core_models import AccessGroup, User, UserAccessGroupLink
from app.models.users import AccountProfileUpdate, UserCreate, UserUpdate
from app.utils.permissions import AccessGroupType, PlatformRole


def test_platform_admin_can_create_orcidless_human(client, db_engine, auth_as, monkeypatch):
    auth_as("platform-admin", platform_role=PlatformRole.ADMIN)

    response = client.post("/users", json={
        "user_info": {
            "first_name": "Test",
            "last_name": "User",
            "username": "test-user",
        },
        "project_ids": [],
    })

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["unique_id"]) == 26
    assert body["is_service_account"] is False

    with Session(db_engine) as session:
        user = session.exec(select(User).where(User.unique_id == body["unique_id"])).one()
        group = session.exec(select(AccessGroup).where(
            AccessGroup.group_name == body["unique_id"],
            AccessGroup.group_type == AccessGroupType.IDENTITY.value,
        )).one()
        membership = session.exec(select(UserAccessGroupLink).where(
            UserAccessGroupLink.user_id == user.id,
            UserAccessGroupLink.accessgroup_id == group.id,
        )).one()
        assert membership.role == "owner"


def test_user_schemas_reject_service_account_mutation():
    for model, payload in (
        (UserCreate, {"first_name": "A", "last_name": "B", "username": "human-user", "is_service_account": True}),
        (UserUpdate, {"is_service_account": True}),
        (AccountProfileUpdate, {"is_service_account": True}),
    ):
        try:
            model.model_validate(payload)
        except ValueError:
            continue
        raise AssertionError(f"{model.__name__} accepted is_service_account")


def _projects(db_engine):
    from app.models.core_models import DeletionRequest, Project
    with Session(db_engine) as session:
        mfids = {}
        for slug in ("myproj", "doomed"):
            project = Project(project_id=slug, organization="MF", project_lead_orcid="lead")
            session.add(project)
            session.flush()
            session.add(AccessGroup(group_name=project.unique_id, group_type=AccessGroupType.PROJECT.value))
            mfids[slug] = project.unique_id
        session.add(DeletionRequest(resource_id=mfids["doomed"], resource_type="project", requester_id="platform-admin", status="pending"))
        session.commit()
    return mfids


def _create(client, username, project_ids):
    return client.post("/users", json={"user_info": {"first_name": "T", "last_name": "U", "username": username}, "project_ids": project_ids})


def _memberships(db_engine, unique_id):
    with Session(db_engine) as session:
        user = session.exec(select(User).where(User.unique_id == unique_id)).one()
        groups = session.exec(
            select(AccessGroup.group_name)
            .join(UserAccessGroupLink, UserAccessGroupLink.accessgroup_id == AccessGroup.id)
            .where(UserAccessGroupLink.user_id == user.id, AccessGroup.group_type == AccessGroupType.PROJECT.value)
        ).all()
        return sorted(groups)


def test_create_user_resolves_projects_like_every_project_reference(client, db_engine, auth_as, monkeypatch):
    auth_as("platform-admin", platform_role=PlatformRole.ADMIN)
    mfids = _projects(db_engine)

    by_case = _create(client, "case-user", ["MyProj", "myproj"])
    assert by_case.status_code == 200, by_case.text
    assert _memberships(db_engine, by_case.json()["unique_id"]) == [mfids["myproj"]]

    by_mfid = _create(client, "mfid-user", [mfids["myproj"]])
    assert by_mfid.status_code == 200, by_mfid.text
    assert _memberships(db_engine, by_mfid.json()["unique_id"]) == [mfids["myproj"]]

    unknown = _create(client, "unknown-user", ["myproj", "nope"])
    assert unknown.status_code == 404
    assert "nope" in unknown.json()["detail"]

    doomed = _create(client, "doomed-user", ["doomed"])
    assert doomed.status_code == 409
    assert doomed.json()["detail"]["code"] == "pending_deletion"
    with Session(db_engine) as session:
        assert session.exec(select(User).where(User.username.in_(["unknown-user", "doomed-user"]))).all() == []


def test_proposal_projects_skip_unknown_and_pending_deletion(client, db_engine, auth_as, monkeypatch):
    monkeypatch.setattr(users_routes, "find_mf_user_proposals", lambda orcid: ["MYPROJ", "doomed", "gone"])
    auth_as("platform-admin", platform_role=PlatformRole.ADMIN)
    mfids = _projects(db_engine)

    response = client.post("/users", json={"user_info": {"unique_id": "0000-0001-0000-0042", "first_name": "T", "last_name": "U", "username": "proposal-user"}})
    assert response.status_code == 200, response.text
    assert _memberships(db_engine, "0000-0001-0000-0042") == [mfids["myproj"]]

import pytest
from fastapi import HTTPException
from sqlmodel import Session, select

from app.models.core_models import (
    AccessGroup,
    Dataset,
    Project,
    Resource,
    ResourceACL,
    User,
    UserAccessGroupLink,
)
from app.api.routes.projects import list_projects
from app.utils.auth_utils import access_selector_filter
from app.utils.permissions import AccessGroupType, GroupMembership, PlatformRole, Principal, Role


def _seed(db_engine):
    with Session(db_engine) as session:
        alice = User(first_name="Alice", last_name="User", email="alice@example.com", username="alice", unique_id="0000-0001-1111-1111")
        bob = User(first_name="Bob", last_name="User", email="bob@example.com", username="bob-user", unique_id="0000-0002-2222-2222")
        session.add_all([alice, bob])
        session.commit()
        session.refresh(alice)
        session.refresh(bob)

        alice_group = AccessGroup(group_name=alice.unique_id, group_type=AccessGroupType.IDENTITY.value)
        bob_group = AccessGroup(group_name=bob.unique_id, group_type=AccessGroupType.IDENTITY.value)
        project_x = Project(project_id="project-x", organization="LBNL", project_lead_orcid=alice.unique_id)
        project_y = Project(project_id="project-y", organization="LBNL", project_lead_orcid=alice.unique_id)
        session.add_all([alice_group, bob_group, project_x, project_y])
        session.commit()
        session.refresh(alice_group)
        session.refresh(bob_group)
        session.refresh(project_x)
        session.refresh(project_y)

        project_x_group = AccessGroup(group_name=project_x.unique_id, group_type=AccessGroupType.PROJECT.value)
        project_y_group = AccessGroup(group_name=project_y.unique_id, group_type=AccessGroupType.PROJECT.value)
        session.add_all([project_x_group, project_y_group])
        session.commit()
        session.refresh(project_x_group)
        session.refresh(project_y_group)

        session.add_all([
            UserAccessGroupLink(user_id=alice.id, accessgroup_id=alice_group.id, role=Role.OWNER.db_value),
            UserAccessGroupLink(user_id=bob.id, accessgroup_id=bob_group.id, role=Role.OWNER.db_value),
            UserAccessGroupLink(user_id=alice.id, accessgroup_id=project_x_group.id, role=Role.VIEWER.db_value),
            UserAccessGroupLink(user_id=alice.id, accessgroup_id=project_y_group.id, role=Role.VIEWER.db_value),
        ])
        session.add_all([
            Resource(mfid=project_x.unique_id, resource_type="project"),
            Resource(mfid=project_y.unique_id, resource_type="project"),
            ResourceACL(mfid=project_x.unique_id, accessgroup_id=project_x_group.id, read=True, write=True, permission=Role.OWNER.db_value),
            ResourceACL(mfid=project_y.unique_id, accessgroup_id=project_y_group.id, read=True, write=True, permission=Role.OWNER.db_value),
            ResourceACL(mfid=project_y.unique_id, accessgroup_id=project_x_group.id, read=True, write=False, permission=Role.VIEWER.db_value),
        ])

        both_users = Dataset(unique_id="ds-both-users", dataset_name="both users")
        both_projects = Dataset(unique_id="ds-both-projects", dataset_name="both projects")
        alice_only = Dataset(unique_id="ds-alice-only", dataset_name="alice only")
        session.add_all([both_users, both_projects, alice_only])
        session.add_all([
            Resource(mfid=both_users.unique_id, resource_type="dataset"),
            Resource(mfid=both_projects.unique_id, resource_type="dataset"),
            Resource(mfid=alice_only.unique_id, resource_type="dataset"),
        ])
        session.add_all([
            ResourceACL(mfid=both_users.unique_id, accessgroup_id=alice_group.id, read=True, write=False, permission=Role.VIEWER.db_value),
            ResourceACL(mfid=both_users.unique_id, accessgroup_id=bob_group.id, read=True, write=False, permission=Role.VIEWER.db_value),
            ResourceACL(mfid=alice_only.unique_id, accessgroup_id=alice_group.id, read=True, write=False, permission=Role.VIEWER.db_value),
            ResourceACL(mfid=both_projects.unique_id, accessgroup_id=project_x_group.id, read=True, write=False, permission=Role.VIEWER.db_value),
            ResourceACL(mfid=both_projects.unique_id, accessgroup_id=project_y_group.id, read=True, write=False, permission=Role.VIEWER.db_value),
        ])
        session.commit()
        return {
            "project_x_mfid": project_x.unique_id,
            "project_x_group_id": project_x_group.id,
            "project_y_mfid": project_y.unique_id,
            "project_y_group_id": project_y_group.id,
        }


def test_user_selectors_intersect_effective_access(db_engine):
    _seed(db_engine)
    caller = Principal(user_id="admin", platform_role=PlatformRole.ADMIN)

    with Session(db_engine) as session:
        clause = access_selector_filter(
            session,
            caller,
            Dataset,
            user_refs=["alice@example.com", "bob-user"],
        )
        rows = session.exec(select(Dataset.unique_id).where(clause)).all()

    assert rows == ["ds-both-users"]


def test_project_selectors_accept_slug_and_mfid_and_intersect(db_engine):
    seeded = _seed(db_engine)
    caller = Principal(
        user_id="0000-0001-1111-1111",
        groups=(
            GroupMembership(id=seeded["project_x_group_id"], group_name=seeded["project_x_mfid"], role=Role.VIEWER),
            GroupMembership(id=seeded["project_y_group_id"], group_name=seeded["project_y_mfid"], role=Role.VIEWER),
        ),
    )

    with Session(db_engine) as session:
        clause = access_selector_filter(
            session,
            caller,
            Dataset,
            project_refs=["project-x", seeded["project_y_mfid"]],
        )
        rows = session.exec(select(Dataset.unique_id).where(clause)).all()

    assert rows == ["ds-both-projects"]


def test_project_selector_requires_membership_and_limits_selector_count(db_engine):
    _seed(db_engine)
    caller = Principal(user_id="outsider")

    with Session(db_engine) as session:
        with pytest.raises(HTTPException) as unauthorized:
            access_selector_filter(session, caller, Dataset, project_refs=["project-x"])
        with pytest.raises(HTTPException) as excessive:
            access_selector_filter(session, caller, Dataset, user_refs=["alice"] * 11)

    assert unauthorized.value.status_code == 403
    assert excessive.value.status_code == 422


def test_project_collection_selector_includes_owned_and_shared_projects(db_engine):
    seeded = _seed(db_engine)
    caller = Principal(
        user_id="0000-0001-1111-1111",
        groups=(
            GroupMembership(id=seeded["project_x_group_id"], group_name=seeded["project_x_mfid"], role=Role.VIEWER),
            GroupMembership(id=seeded["project_y_group_id"], group_name=seeded["project_y_mfid"], role=Role.VIEWER),
        ),
    )

    response = list_projects(
        auth_res=caller,
        project_id=None,
        accessible_to_user=None,
        accessible_to_project=["project-x"],
        include_metadata=False,
        include_members=False,
        limit=100,
        offset=0,
    )

    assert {project.project_id for project in response.items} == {"project-x", "project-y"}
    assert all(not hasattr(project.lead, "email") for project in response.items)


def test_exact_project_slug_collection_can_include_members(db_engine):
    seeded = _seed(db_engine)
    caller = Principal(
        user_id="0000-0001-1111-1111",
        groups=(
            GroupMembership(id=seeded["project_x_group_id"], group_name=seeded["project_x_mfid"], role=Role.VIEWER),
        ),
    )

    response = list_projects(
        auth_res=caller,
        project_id="project-x",
        accessible_to_user=None,
        accessible_to_project=None,
        include_metadata=False,
        include_members=True,
        limit=100,
        offset=0,
    )

    assert response.total == 1
    assert [member.unique_id for member in response.items[0].members] == ["0000-0001-1111-1111"]


def test_project_collection_member_expansion_requires_exact_slug():
    caller = Principal(user_id="admin", platform_role=PlatformRole.ADMIN)

    with pytest.raises(HTTPException) as error:
        list_projects(
            auth_res=caller,
            project_id=None,
            accessible_to_user=None,
            accessible_to_project=None,
            include_metadata=False,
            include_members=True,
            limit=100,
            offset=0,
        )

    assert error.value.status_code == 422


def test_access_selectors_are_repeated_openapi_arrays():
    from app.api.main import app

    schema = app.openapi()
    for path in ("/datasets", "/samples", "/projects"):
        parameters = {
            parameter["name"]: parameter
            for parameter in schema["paths"][path]["get"]["parameters"]
        }
        assert parameters["accessible_to_user"]["schema"]["anyOf"][0]["type"] == "array"
        assert parameters["accessible_to_project"]["schema"]["anyOf"][0]["type"] == "array"
        if path == "/projects":
            assert parameters["include_members"]["schema"]["default"] is False
        assert "accessible_to" not in parameters

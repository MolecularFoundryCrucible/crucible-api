from sqlmodel import Session, select

from app.tables import (
    AccessGroup,
    Dataset,
    Project,
    Resource,
    ResourceACL,
    Sample,
    User,
    UserAccessGroupLink,
)
from app.utils.permissions import AccessGroupType, PlatformRole, Role


TARGET_ID = "0000-0001-1111-1111"
OTHER_ID = "0000-0002-2222-2222"


def _setup(db_engine):
    with Session(db_engine) as session:
        target = User(
            first_name="Target",
            last_name="User",
            email="target@example.com",
            username="target-user",
            unique_id=TARGET_ID,
        )
        other = User(
            first_name="Other",
            last_name="User",
            email="other@example.com",
            username="other-user",
            unique_id=OTHER_ID,
        )
        target_group = AccessGroup(group_name="target-access")
        other_group = AccessGroup(group_name="other-access")
        session.add_all([target, other, target_group, other_group])
        session.commit()
        session.refresh(target)
        session.refresh(other)
        session.refresh(target_group)
        session.refresh(other_group)
        session.add_all([
            UserAccessGroupLink(
                user_id=target.id,
                accessgroup_id=target_group.id,
                role=Role.VIEWER.db_value,
            ),
            UserAccessGroupLink(
                user_id=other.id,
                accessgroup_id=other_group.id,
                role=Role.VIEWER.db_value,
            ),
        ])

        target_dataset = Dataset(unique_id="ds_target_access", dataset_name="target dataset")
        other_dataset = Dataset(unique_id="ds_other_access", dataset_name="other dataset")
        target_sample = Sample(unique_id="sm_target_access", sample_name="target sample")
        other_sample = Sample(unique_id="sm_other_access", sample_name="other sample")
        session.add_all([
            target_dataset,
            other_dataset,
            target_sample,
            other_sample,
            Resource(mfid=target_dataset.unique_id, resource_type="dataset"),
            Resource(mfid=other_dataset.unique_id, resource_type="dataset"),
            Resource(mfid=target_sample.unique_id, resource_type="sample"),
            Resource(mfid=other_sample.unique_id, resource_type="sample"),
            ResourceACL(
                mfid=target_dataset.unique_id,
                accessgroup_id=target_group.id,
                read=True,
                write=False,
                permission=Role.VIEWER.db_value,
            ),
            ResourceACL(
                mfid=other_dataset.unique_id,
                accessgroup_id=other_group.id,
                read=True,
                write=False,
                permission=Role.VIEWER.db_value,
            ),
            ResourceACL(
                mfid=target_sample.unique_id,
                accessgroup_id=target_group.id,
                read=True,
                write=False,
                permission=Role.VIEWER.db_value,
            ),
            ResourceACL(
                mfid=other_sample.unique_id,
                accessgroup_id=other_group.id,
                read=True,
                write=False,
                permission=Role.VIEWER.db_value,
            ),
        ])
        session.commit()
        return target_group.id


def _add_project(db_engine, slug):
    with Session(db_engine) as session:
        target = session.exec(select(User).where(User.unique_id == TARGET_ID)).one()
        project = Project(
            project_id=slug,
            organization="LBNL",
            project_lead_orcid=target.unique_id,
            lead_user_id=target.id,
        )
        session.add(project)
        session.commit()
        session.refresh(project)
        group = AccessGroup(
            group_name=project.unique_id,
            group_type=AccessGroupType.PROJECT.value,
        )
        session.add(group)
        session.add(Resource(mfid=project.unique_id, resource_type="project"))
        session.commit()
        session.refresh(group)
        session.add(UserAccessGroupLink(
            user_id=target.id,
            accessgroup_id=group.id,
            role=Role.VIEWER.db_value,
        ))
        session.add(ResourceACL(
            mfid=project.unique_id,
            accessgroup_id=group.id,
            read=True,
            write=True,
            permission=Role.OWNER.db_value,
        ))
        session.commit()
        return project.unique_id, group.id


def test_platform_admin_can_list_resources_accessible_to_another_user(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as("platform-admin", platform_role=PlatformRole.ADMIN)

    datasets = client.get("/datasets", params={"accessible_to_user": "target-user"})
    samples = client.get("/samples", params={"accessible_to_user": "target@example.com"})

    assert datasets.status_code == 200, datasets.text
    assert samples.status_code == 200, samples.text
    assert [item["unique_id"] for item in datasets.json()["items"]] == ["ds_target_access"]
    assert [item["unique_id"] for item in samples.json()["items"]] == ["sm_target_access"]


def test_service_account_can_list_resources_accessible_to_another_user(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as(
        "service-account",
        groups=[],
        platform_role=PlatformRole.CONTRIBUTOR,
        is_service_account=True,
    )

    datasets = client.get("/datasets", params={"accessible_to_user": "target-user"})
    samples = client.get("/samples", params={"accessible_to_user": TARGET_ID})

    assert datasets.status_code == 200, datasets.text
    assert samples.status_code == 200, samples.text
    assert [item["unique_id"] for item in datasets.json()["items"]] == ["ds_target_access"]
    assert [item["unique_id"] for item in samples.json()["items"]] == ["sm_target_access"]


def test_service_account_can_list_another_users_projects(client, db_engine, auth_as):
    _setup(db_engine)
    project_mfid, _ = _add_project(db_engine, "target-project")
    auth_as(
        "service-account",
        groups=[],
        platform_role=PlatformRole.CONTRIBUTOR,
        is_service_account=True,
    )

    response = client.get("/projects", params={"accessible_to_user": "target@example.com"})

    assert response.status_code == 200, response.text
    assert [item["unique_id"] for item in response.json()["items"]] == [project_mfid]


def test_user_can_explicitly_filter_to_self(client, db_engine, auth_as):
    group_id = _setup(db_engine)
    auth_as(TARGET_ID, groups=[("target-access", group_id, Role.VIEWER)])

    response = client.get("/datasets", params={"accessible_to_user": "target-user"})

    assert response.status_code == 200, response.text
    assert [item["unique_id"] for item in response.json()["items"]] == ["ds_target_access"]


def test_user_cannot_inspect_another_users_access(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as(OTHER_ID)

    response = client.get("/datasets", params={"accessible_to_user": "target-user"})

    assert response.status_code == 403, response.text


def test_repeated_user_selectors_intersect_effective_access(client, db_engine, auth_as):
    _setup(db_engine)
    with Session(db_engine) as session:
        target_group = session.exec(select(AccessGroup).where(AccessGroup.group_name == "target-access")).one()
        other_group = session.exec(select(AccessGroup).where(AccessGroup.group_name == "other-access")).one()
        shared = Dataset(unique_id="ds_shared_users", dataset_name="shared users")
        session.add(shared)
        session.add(Resource(mfid=shared.unique_id, resource_type="dataset"))
        session.add_all([
            ResourceACL(
                mfid=shared.unique_id,
                accessgroup_id=target_group.id,
                read=True,
                write=False,
                permission=Role.VIEWER.db_value,
            ),
            ResourceACL(
                mfid=shared.unique_id,
                accessgroup_id=other_group.id,
                read=True,
                write=False,
                permission=Role.VIEWER.db_value,
            ),
        ])
        session.commit()
    auth_as("platform-admin", platform_role=PlatformRole.ADMIN)

    response = client.get("/datasets", params=[
        ("accessible_to_user", "target-user"),
        ("accessible_to_user", "other-user"),
    ])

    assert response.status_code == 200, response.text
    assert [item["unique_id"] for item in response.json()["items"]] == ["ds_shared_users"]


def test_repeated_project_selectors_intersect_direct_access(client, db_engine, auth_as):
    _setup(db_engine)
    project_x_mfid, project_x_group_id = _add_project(db_engine, "project-x")
    project_y_mfid, project_y_group_id = _add_project(db_engine, "project-y")
    with Session(db_engine) as session:
        shared = Dataset(unique_id="ds_shared_projects", dataset_name="shared projects")
        session.add(shared)
        session.add(Resource(mfid=shared.unique_id, resource_type="dataset"))
        session.add_all([
            ResourceACL(
                mfid=shared.unique_id,
                accessgroup_id=project_x_group_id,
                read=True,
                write=False,
                permission=Role.VIEWER.db_value,
            ),
            ResourceACL(
                mfid=shared.unique_id,
                accessgroup_id=project_y_group_id,
                read=True,
                write=False,
                permission=Role.VIEWER.db_value,
            ),
        ])
        session.commit()
    auth_as(TARGET_ID, groups=[
        (project_x_mfid, project_x_group_id, Role.VIEWER),
        (project_y_mfid, project_y_group_id, Role.VIEWER),
    ])

    response = client.get("/datasets", params=[
        ("accessible_to_project", "project-x"),
        ("accessible_to_project", project_y_mfid),
    ])

    assert response.status_code == 200, response.text
    assert [item["unique_id"] for item in response.json()["items"]] == ["ds_shared_projects"]


def test_project_selector_filters_project_collection_and_preserves_redaction(client, db_engine, auth_as):
    _setup(db_engine)
    project_x_mfid, project_x_group_id = _add_project(db_engine, "project-x")
    project_y_mfid, _ = _add_project(db_engine, "project-y")
    with Session(db_engine) as session:
        session.add(ResourceACL(
            mfid=project_y_mfid,
            accessgroup_id=project_x_group_id,
            read=True,
            write=False,
            permission=Role.VIEWER.db_value,
        ))
        session.commit()
    auth_as(TARGET_ID, groups=[(project_x_mfid, project_x_group_id, Role.VIEWER)])

    response = client.get("/projects", params={"accessible_to_project": "project-x"})

    assert response.status_code == 200, response.text
    assert {item["project_id"] for item in response.json()["items"]} == {"project-x", "project-y"}
    assert all("email" not in item["lead"] for item in response.json()["items"])


def test_project_selector_requires_membership_or_platform_admin(client, db_engine, auth_as):
    _setup(db_engine)
    _add_project(db_engine, "project-x")
    auth_as(OTHER_ID)

    response = client.get("/datasets", params={"accessible_to_project": "project-x"})

    assert response.status_code == 403, response.text


def test_access_selectors_are_limited(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as(TARGET_ID)

    response = client.get(
        "/datasets",
        params=[("accessible_to_user", TARGET_ID)] * 11,
    )

    assert response.status_code == 422, response.text


def test_the_filter_lists_what_the_removed_user_routes_listed(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as("platform-admin", platform_role=PlatformRole.ADMIN)

    datasets = client.get("/datasets", params={"accessible_to_user": "target-user"})
    samples = client.get("/samples", params={"accessible_to_user": "target-user"})

    assert datasets.status_code == 200, datasets.text
    assert samples.status_code == 200, samples.text
    assert [item["unique_id"] for item in datasets.json()["items"]] == ["ds_target_access"]
    assert [item["unique_id"] for item in samples.json()["items"]] == ["sm_target_access"]


def test_dataset_access_reports_effective_role_capped_by_group_standing(client, db_engine, auth_as):
    group_id = _setup(db_engine)
    with Session(db_engine) as session:
        acl = session.get(ResourceACL, ("ds_target_access", group_id))
        acl.permission = Role.ADMIN.db_value
        acl.write = True
        session.add(acl)
        session.commit()
    auth_as("platform-admin", platform_role=PlatformRole.ADMIN)

    response = client.get("/users/target-user/datasets/ds_target_access")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "resource_mfid": "ds_target_access",
        "user_id": TARGET_ID,
        "effective_access": "viewer",
    }


def test_dataset_access_reports_none_for_unreachable_dataset(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as("platform-admin", platform_role=PlatformRole.ADMIN)

    response = client.get("/users/target-user/datasets/ds_other_access")

    assert response.status_code == 200, response.text
    assert response.json()["effective_access"] == "none"


def test_dataset_access_reports_platform_admin_bypass(client, db_engine, auth_as):
    _setup(db_engine)
    with Session(db_engine) as session:
        target = session.exec(select(User).where(User.unique_id == TARGET_ID)).one()
        target.platform_role = PlatformRole.ADMIN.value
        session.add(target)
        session.commit()
    auth_as("platform-admin", platform_role=PlatformRole.ADMIN)

    response = client.get("/users/target-user/datasets/ds_other_access")

    assert response.status_code == 200, response.text
    assert response.json()["effective_access"] == "platform_admin"


def test_dataset_access_reports_service_account_contributor_ceiling(client, db_engine, auth_as):
    _setup(db_engine)
    with Session(db_engine) as session:
        target = session.exec(select(User).where(User.unique_id == TARGET_ID)).one()
        target.is_service_account = True
        target.platform_role = PlatformRole.CONTRIBUTOR.value
        session.add(target)
        session.commit()
    auth_as("platform-admin", platform_role=PlatformRole.ADMIN)

    response = client.get("/users/target-user/datasets/ds_target_access")

    assert response.status_code == 200, response.text
    assert response.json()["effective_access"] == "contributor"


def test_dataset_access_returns_404_for_missing_dataset(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as("platform-admin", platform_role=PlatformRole.ADMIN)

    response = client.get("/users/target-user/datasets/missing")

    assert response.status_code == 404, response.text

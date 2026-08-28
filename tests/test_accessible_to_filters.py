from sqlmodel import Session, select

from app.models.core_models import (
    AccessGroup,
    Dataset,
    Resource,
    ResourceACL,
    Sample,
    User,
    UserAccessGroupLink,
)
from app.utils.permissions import PlatformRole, Role


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


def test_platform_admin_can_list_resources_accessible_to_another_user(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as("platform-admin", platform_role=PlatformRole.ADMIN)

    datasets = client.get("/datasets", params={"accessible_to": "target-user"})
    samples = client.get("/samples", params={"accessible_to": "target@example.com"})

    assert datasets.status_code == 200, datasets.text
    assert samples.status_code == 200, samples.text
    assert [item["unique_id"] for item in datasets.json()["items"]] == ["ds_target_access"]
    assert [item["unique_id"] for item in samples.json()["items"]] == ["sm_target_access"]


def test_user_can_explicitly_filter_to_self(client, db_engine, auth_as):
    group_id = _setup(db_engine)
    auth_as(TARGET_ID, groups=[("target-access", group_id, Role.VIEWER)])

    response = client.get("/datasets", params={"accessible_to": "target-user"})

    assert response.status_code == 200, response.text
    assert [item["unique_id"] for item in response.json()["items"]] == ["ds_target_access"]


def test_user_cannot_inspect_another_users_access(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as(OTHER_ID)

    response = client.get("/datasets", params={"accessible_to": "target-user"})

    assert response.status_code == 403, response.text


def test_deprecated_user_routes_preserve_mfid_list_contract(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as("platform-admin", platform_role=PlatformRole.ADMIN)

    datasets = client.get("/users/target-user/datasets")
    samples = client.get("/users/target-user/samples")

    assert datasets.status_code == 200, datasets.text
    assert samples.status_code == 200, samples.text
    assert datasets.json() == ["ds_target_access"]
    assert samples.json() == ["sm_target_access"]


def test_user_resource_routes_are_deprecated_in_openapi(client):
    schema = client.get("/openapi.json").json()

    assert schema["paths"]["/users/{user_id}/datasets"]["get"]["deprecated"] is True
    assert schema["paths"]["/users/{user_id}/samples"]["get"]["deprecated"] is True


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


def test_dataset_access_returns_404_for_missing_dataset(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as("platform-admin", platform_role=PlatformRole.ADMIN)

    response = client.get("/users/target-user/datasets/missing")

    assert response.status_code == 404, response.text

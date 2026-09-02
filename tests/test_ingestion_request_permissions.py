from sqlmodel import Session

from app.models.core_models import AccessGroup, Dataset, IngestionRequest, Resource, ResourceACL
from app.utils.permissions import PlatformRole, Role


def _setup_ingestion_request(db_engine, *, public=False, permission=None):
    with Session(db_engine) as session:
        dataset = Dataset(unique_id="ds_ingestion_access", dataset_name="test", public=public)
        session.add_all([
            Resource(mfid=dataset.unique_id, resource_type="dataset", public=public),
            dataset,
        ])
        session.commit()

        group_id = None
        if permission is not None:
            group = AccessGroup(group_name="ingestion-readers")
            session.add(group)
            session.commit()
            session.refresh(group)
            group_id = group.id
            session.add(ResourceACL(
                mfid=dataset.unique_id,
                accessgroup_id=group.id,
                read=True,
                write=permission != Role.VIEWER,
                permission=permission.db_value,
            ))

        request = IngestionRequest(dataset_mfid=dataset.unique_id, status="requested")
        session.add(request)
        session.commit()
        session.refresh(request)
        return request.id, dataset.unique_id, group_id


def test_platform_admin_can_read_ingestion_request_without_group_membership(client, db_engine, auth_as):
    request_id, _, _ = _setup_ingestion_request(db_engine)
    auth_as("platform-admin", platform_role=PlatformRole.ADMIN)

    response = client.get(f"/ingestion_requests/{request_id}")

    assert response.status_code == 200, response.text


def test_legacy_public_boolean_does_not_authorize_ingestion_read(client, db_engine, auth_as):
    request_id, _, _ = _setup_ingestion_request(db_engine, public=True)
    auth_as("ordinary-user")

    response = client.get(f"/ingestion_requests/{request_id}")

    assert response.status_code == 403, response.text


def test_acl_viewer_can_read_and_list_ingestion_requests(client, db_engine, auth_as):
    request_id, dataset_mfid, group_id = _setup_ingestion_request(db_engine, permission=Role.VIEWER)
    auth_as("ordinary-user", groups=[("ingestion-readers", group_id, Role.VIEWER)])

    read_response = client.get(f"/ingestion_requests/{request_id}")
    list_response = client.get("/ingestion_requests", params={"dataset_id": dataset_mfid})

    assert read_response.status_code == 200, read_response.text
    assert list_response.status_code == 200, list_response.text
    assert [item["id"] for item in list_response.json()["items"]] == [request_id]


def test_legacy_public_boolean_does_not_authorize_ingestion_list(client, db_engine, auth_as):
    _, dataset_mfid, _ = _setup_ingestion_request(db_engine, public=True)
    auth_as("ordinary-user")

    response = client.get("/ingestion_requests", params={"dataset_id": dataset_mfid})

    assert response.status_code == 403, response.text

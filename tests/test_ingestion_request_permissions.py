from sqlmodel import Session

from app.models.core_models import (
    AccessGroup,
    AssociatedFile,
    Dataset,
    IngestionRequest,
    Resource,
    ResourceACL,
)
from app.utils.permissions import PlatformRole, Role


def _setup_ingestion_request(db_engine, *, public=False, permission=None, suffix=""):
    with Session(db_engine) as session:
        dataset = Dataset(
            unique_id=f"ds_ingestion_access{suffix}",
            dataset_name="test",
            public=public,
        )
        session.add_all([
            Resource(mfid=dataset.unique_id, resource_type="dataset", public=public),
            dataset,
        ])
        session.commit()

        group_id = None
        if permission is not None:
            group = AccessGroup(group_name=f"ingestion-readers{suffix}")
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


def _setup_file_ingestion_request(db_engine):
    with Session(db_engine) as session:
        dataset = Dataset(
            unique_id="ds_file_filter",
            dataset_name="test",
            public=False,
        )
        session.add_all([
            Resource(mfid=dataset.unique_id, resource_type="dataset", public=False),
            dataset,
        ])
        session.commit()

        group = AccessGroup(group_name="file-filter-readers")
        session.add(group)
        session.commit()
        session.refresh(group)
        session.add(ResourceACL(
            mfid=dataset.unique_id,
            accessgroup_id=group.id,
            read=True,
            write=False,
            permission=Role.VIEWER.db_value,
        ))

        associated = AssociatedFile(
            filename="data.tif",
            size=1,
            sha256_hash="0" * 64,
            dataset_id=dataset.id,
        )
        session.add(associated)
        session.commit()
        session.refresh(associated)

        file_request = IngestionRequest(
            dataset_mfid=dataset.unique_id,
            file_mfid=associated.mfid,
            status="requested",
        )
        session.add(file_request)
        session.commit()
        session.refresh(file_request)
        return associated.mfid, file_request.id, group.id


def test_platform_admin_can_read_ingestion_request_without_group_membership(client, db_engine, auth_as):
    request_id, _, _ = _setup_ingestion_request(db_engine)
    auth_as("platform-admin", platform_role=PlatformRole.ADMIN)

    response = client.get(f"/ingestion_requests/{request_id}")

    assert response.status_code == 200, response.text


def test_legacy_public_boolean_does_not_authorize_ingestion_read(client, db_engine, auth_as):
    request_id, _, _ = _setup_ingestion_request(db_engine, public=True)
    auth_as("ordinary-user")

    response = client.get(f"/ingestion_requests/{request_id}")

    assert response.status_code == 404, response.text


def test_acl_viewer_can_read_and_list_ingestion_requests(client, db_engine, auth_as):
    request_id, dataset_mfid, group_id = _setup_ingestion_request(db_engine, permission=Role.VIEWER)
    auth_as("ordinary-user", groups=[("ingestion-readers", group_id, Role.VIEWER)])

    read_response = client.get(f"/ingestion_requests/{request_id}")
    list_response = client.get("/ingestion_requests", params={"dataset_id": dataset_mfid})

    assert read_response.status_code == 200, read_response.text
    assert list_response.status_code == 200, list_response.text
    assert [item["id"] for item in list_response.json()["items"]] == [request_id]


def test_dataset_mfid_filter_matches_dataset_id_alias(client, db_engine, auth_as):
    request_id, dataset_mfid, group_id = _setup_ingestion_request(db_engine, permission=Role.VIEWER)
    auth_as("ordinary-user", groups=[("ingestion-readers", group_id, Role.VIEWER)])

    preferred = client.get("/ingestion_requests", params={"dataset_mfid": dataset_mfid})
    alias = client.get("/ingestion_requests", params={"dataset_id": dataset_mfid})

    assert preferred.status_code == 200, preferred.text
    assert alias.status_code == 200, alias.text
    assert [item["id"] for item in preferred.json()["items"]] == [request_id]
    assert [item["id"] for item in alias.json()["items"]] == [request_id]


def test_file_mfid_filter_matches_file_id_alias(client, db_engine, auth_as):
    file_mfid, request_id, group_id = _setup_file_ingestion_request(db_engine)
    auth_as("ordinary-user", groups=[("file-filter-readers", group_id, Role.VIEWER)])

    preferred = client.get("/ingestion_requests", params={"file_mfid": file_mfid})
    alias = client.get("/ingestion_requests", params={"file_id": file_mfid})

    assert preferred.status_code == 200, preferred.text
    assert alias.status_code == 200, alias.text
    assert [item["id"] for item in preferred.json()["items"]] == [request_id]
    assert [item["id"] for item in alias.json()["items"]] == [request_id]


def test_file_filter_takes_precedence_over_dataset_filter(client, db_engine, auth_as):
    file_mfid, file_request_id, file_group_id = _setup_file_ingestion_request(db_engine)
    other_request_id, other_dataset_mfid, other_group_id = _setup_ingestion_request(
        db_engine, permission=Role.VIEWER, suffix="_other"
    )
    auth_as("ordinary-user", groups=[
        ("file-filter-readers", file_group_id, Role.VIEWER),
        ("ingestion-readers_other", other_group_id, Role.VIEWER),
    ])

    response = client.get("/ingestion_requests", params={
        "file_mfid": file_mfid,
        "dataset_mfid": other_dataset_mfid,
    })

    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["items"]] == [file_request_id]


def test_acl_viewer_unfiltered_list_only_includes_accessible_ingestion_requests(
    client, db_engine, auth_as
):
    accessible_id, _, group_id = _setup_ingestion_request(
        db_engine, permission=Role.VIEWER, suffix="_accessible"
    )
    _setup_ingestion_request(db_engine, permission=Role.VIEWER, suffix="_inaccessible")
    auth_as(
        "ordinary-user",
        groups=[("ingestion-readers_accessible", group_id, Role.VIEWER)],
    )

    response = client.get("/ingestion_requests")

    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1
    assert [item["id"] for item in response.json()["items"]] == [accessible_id]


def test_legacy_public_boolean_does_not_authorize_ingestion_list(client, db_engine, auth_as):
    _, dataset_mfid, _ = _setup_ingestion_request(db_engine, public=True)
    auth_as("ordinary-user")

    response = client.get("/ingestion_requests", params={"dataset_id": dataset_mfid})

    assert response.status_code == 404, response.text


def test_patch_records_the_outcome_and_ignores_other_fields(client, db_engine, auth_as):
    request_id, dataset_mfid, group_id = _setup_ingestion_request(db_engine, permission=Role.EDITOR)
    auth_as("ordinary-user", groups=[("ingestion-readers", group_id, Role.EDITOR)])
    answer = client.patch(f"/ingestion_requests/{request_id}", json={
        "status": "completed", "time_completed": "2026-11-09T10:00:00Z", "ingestion_githash": "abc123",
        "file_to_upload": "other-bucket/secret.csv", "ingestion_class": "../../evil", "time_created": "1970-01-01",
    })
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert (body["status"], body["time_completed"], body["ingestion_githash"]) == ("completed", "2026-11-09T10:00:00Z", "abc123")
    assert (body["file_to_upload"], body["ingestion_class"], body["time_created"]) == (None, None, None)
    assert client.patch(f"/ingestion_requests/{request_id}", json={"dataset_mfid": "elsewhere"}).status_code == 400
    assert client.patch(f"/ingestion_requests/{request_id}", json={"status": "x" * 65}).status_code == 422


def test_ingest_refuses_an_ingestor_name_with_unexpected_characters(client, db_engine, auth_as):
    auth_as("ordinary-user", platform_role=PlatformRole.ADMIN)
    for name in ("../ingestors/evil", "AFM Ingestor", "x" * 129, "os.system('id')"):
        assert client.post("/files/any-file/ingest", params={"ingestion_class": name}).status_code == 422, name

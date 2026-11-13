"""Record responses share one shape: owner as a user, nested project and instrument references, role,
and full items for related records without nested links."""
from tests.test_instrument_members import OWNER, SCIENTIST, VIEWER, _as, _world


def test_lists_and_related_routes_return_full_items(client, db_engine, auth_as):
    mfid, dataset = _world(client, db_engine, auth_as)
    _as(auth_as, db_engine, SCIENTIST)
    child = client.post("/datasets", json={"dataset_name": "child", "instrument_mfid": mfid}).json()["unique_id"]
    assert client.post(f"/datasets/{dataset}/children/{child}").status_code in (200, 201)
    listed = client.get("/datasets").json()["items"][0]
    assert listed["owner"]["unique_id"] == SCIENTIST and listed["role"] == "owner"
    for path in (f"/datasets/{dataset}/children", f"/datasets/{child}/parents"):
        item = client.get(path, params={"include_links": True}).json()["items"][0]
        assert item["owner"]["unique_id"] == SCIENTIST
        assert item["instrument"]["unique_id"] == mfid
        assert item["role"] == "owner" and item["creation_time"] is not None
        assert item["links"] is None
    assert client.get(f"/datasets/{dataset}/children", params={"include_owner": False}).json()["items"][0]["owner"] is None


def test_sample_embedding_is_off_by_default_and_full_when_asked(client, db_engine, auth_as):
    _, dataset = _world(client, db_engine, auth_as)
    _as(auth_as, db_engine, SCIENTIST)
    sample = client.post("/samples", json={"sample_name": "s"}).json()["unique_id"]
    assert client.post(f"/datasets/{dataset}/samples/{sample}").status_code in (200, 201)
    assert client.get(f"/samples/{sample}").json()["datasets"] is None
    embedded = client.get(f"/samples/{sample}", params={"include_datasets": True}).json()["datasets"][0]
    assert embedded["owner"]["unique_id"] == SCIENTIST and embedded["links"] is None


def test_role_reflects_instrument_membership(client, db_engine, auth_as):
    _, dataset = _world(client, db_engine, auth_as)
    _as(auth_as, db_engine, VIEWER)
    assert client.get(f"/datasets/{dataset}").json()["role"] == "viewer"


def test_projects_have_owner_resource_type_and_one_model_for_list_and_search(client, db_engine, auth_as):
    _world(client, db_engine, auth_as)
    _as(auth_as, db_engine, OWNER)
    project = client.post("/projects", json={"project_id": "resp-proj", "organization": "MF", "owner_ref": OWNER}).json()["unique_id"]
    _as(auth_as, db_engine, OWNER)
    detail = client.get(f"/projects/{project}").json()
    assert detail["owner"]["unique_id"] == OWNER and detail["lead"] == detail["owner"]
    assert detail["resource_type"] == "project"
    schema = client.get("/openapi.json").json()
    search = schema["paths"]["/projects/search"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    listing = schema["paths"]["/projects"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    assert search == listing


def test_deletion_status_is_typed_without_internal_ids(client, db_engine, auth_as):
    _, dataset = _world(client, db_engine, auth_as)
    _as(auth_as, db_engine, SCIENTIST)
    assert client.post(f"/resources/{dataset}/delete", json={"reason": "duplicate"}).status_code in (200, 201)
    status = client.get(f"/datasets/{dataset}").json()["deletion_request"]
    assert status["resource_mfid"] == dataset and status["status"] == "pending"
    assert "requester_user_id" not in status and "reviewer_user_id" not in status
    schema = client.get("/openapi.json").json()["components"]["schemas"]["DatasetResponse"]["properties"]["deletion_request"]
    assert schema["anyOf"][0]["$ref"].endswith("ActiveDeletionRequestRead")


def test_flat_reference_fields_are_deprecated(client):
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    for model, fields in (("DatasetResponse", ("owner_orcid", "project_id", "instrument_id", "instrument_name")),
                          ("SampleResponse", ("owner_orcid", "project_id")),
                          ("InstrumentResponse", ("owner_orcid",)),
                          ("ProjectWithLead", ("lead", "project_lead_orcid"))):
        for field in fields:
            assert schemas[model]["properties"][field].get("deprecated") is True, (model, field)

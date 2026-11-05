from sqlmodel import Session

from app.models.core_models import User
from app.utils.permissions import PlatformRole
from tests.people import internal_id


PERSON_ORCID = "0000-0001-2345-6789"
OTHER_ORCID = "0000-0002-3456-7890"
SERVICE_ACCOUNT_MFID = "0tkn2knjast3h0008nyq9zps2c"


def _setup(db_engine):
    with Session(db_engine) as session:
        session.add(User(
            first_name="Alice",
            last_name="Researcher",
            email="Alice@Example.com",
            unique_id=PERSON_ORCID,
            username="alice_researcher",
        ))
        session.add(User(
            first_name="Other",
            last_name="User",
            email="other@example.com",
            unique_id=OTHER_ORCID,
            username="other_user",
        ))
        session.add(User(
            first_name="Service",
            last_name="Account",
            email=None,
            unique_id=SERVICE_ACCOUNT_MFID,
            username="beamline_service",
            is_service_account=True,
        ))
        session.commit()


def test_canonical_orcid_get_returns_private_profile_to_self(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as(PERSON_ORCID)

    response = client.get(f"/users/{PERSON_ORCID}")

    assert response.status_code == 200, response.text
    assert response.json()["email"] == "Alice@Example.com"
    assert "content-location" not in response.headers


def test_canonical_orcid_get_returns_public_profile_to_other_user(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as(OTHER_ORCID)

    response = client.get(f"/users/{PERSON_ORCID}")

    assert response.status_code == 200, response.text
    assert response.json()["unique_id"] == PERSON_ORCID
    assert "email" not in response.json()


def test_canonical_service_account_mfid_get_works_for_admin(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as(OTHER_ORCID, platform_role=PlatformRole.ADMIN)

    response = client.get(f"/users/{SERVICE_ACCOUNT_MFID}")

    assert response.status_code == 200, response.text
    assert response.json()["unique_id"] == SERVICE_ACCOUNT_MFID
    assert "content-location" not in response.headers


def test_username_compatibility_get_returns_public_profile_and_location(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as(OTHER_ORCID)

    response = client.get("/users/alice_researcher")

    assert response.status_code == 200, response.text
    assert response.json()["unique_id"] == PERSON_ORCID
    assert "email" not in response.json()
    assert response.headers["content-location"] == f"/users/{PERSON_ORCID}"


def test_email_compatibility_get_is_case_insensitive(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as(OTHER_ORCID)

    response = client.get("/users/ALICE@example.COM")

    assert response.status_code == 200, response.text
    assert response.json()["unique_id"] == PERSON_ORCID
    assert "email" not in response.json()
    assert response.headers["content-location"] == f"/users/{PERSON_ORCID}"


def test_by_username_compatibility_get_returns_canonical_location(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as(OTHER_ORCID)

    response = client.get("/users/by-username/alice_researcher")

    assert response.status_code == 200, response.text
    assert response.json()["unique_id"] == PERSON_ORCID
    assert "email" not in response.json()
    assert response.headers["content-location"] == f"/users/{PERSON_ORCID}"


def test_missing_singleton_returns_404(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as(OTHER_ORCID)

    response = client.get("/users/missing_user")

    assert response.status_code == 404


def test_exact_username_query_returns_public_profile(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as(OTHER_ORCID)

    response = client.get("/users", params={"username": "ALICE_RESEARCHER"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["unique_id"] == PERSON_ORCID
    assert "email" not in body["items"][0]


def test_exact_username_query_returns_private_profile_to_self(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as(PERSON_ORCID)

    response = client.get("/users", params={"username": "alice_researcher"})

    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["email"] == "Alice@Example.com"


def test_exact_email_query_is_case_insensitive(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as(OTHER_ORCID)

    response = client.get("/users", params={"email": "ALICE@example.COM"})

    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["unique_id"] == PERSON_ORCID
    assert "email" not in response.json()["items"][0]


def test_exact_unique_id_collection_query_returns_private_profile_to_self(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as(PERSON_ORCID)

    response = client.get("/users", params={"unique_id": PERSON_ORCID})

    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["email"] == "Alice@Example.com"


def test_exact_email_query_returns_private_profile_to_admin(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as(OTHER_ORCID, platform_role=PlatformRole.ADMIN)

    response = client.get("/users", params={"email": "alice@example.com"})

    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["email"] == "Alice@Example.com"


def test_canonical_user_get_returns_private_profile_to_admin(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as(OTHER_ORCID, platform_role=PlatformRole.ADMIN)

    response = client.get(f"/users/{PERSON_ORCID}")

    assert response.status_code == 200, response.text
    assert response.json()["email"] == "Alice@Example.com"


def test_permissive_does_not_make_username_identity_query_partial(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as(OTHER_ORCID)

    response = client.get("/users", params={"username": "alice", "permissive": "true"})

    assert response.status_code == 200, response.text
    assert response.json()["total"] == 0
    assert response.json()["items"] == []


def test_user_routes_document_user_ref(client):
    schema = client.get("/openapi.json").json()
    operations = (
        ("/users/{user_ref}", "get"),
        ("/users/{user_ref}", "patch"),
        ("/users/{user_ref}/apikey", "post"),
        ("/users/{user_ref}/apikey/verify", "get"),
        ("/users/{user_ref}/apikey", "delete"),
        ("/users/{user_ref}/platform_role", "patch"),
        ("/users/{user_ref}/datasets", "get"),
        ("/users/{user_ref}/samples", "get"),
        ("/users/{user_ref}/datasets/{dataset_mfid}", "get"),
        ("/users/{user_ref}/access_groups/{group_name}", "post"),
        ("/users/{user_ref}/access_groups", "get"),
        ("/users/{user_ref}/access_groups/{group_name}", "delete"),
        ("/users/{user_ref}/projects", "get"),
        ("/projects/{project_ref}/users/{user_ref}", "post"),
        ("/projects/{project_ref}/users/{user_ref}", "patch"),
        ("/projects/{project_ref}/users/{user_ref}", "delete"),
    )

    for path, method in operations:
        parameters = {
            parameter["name"]: parameter
            for parameter in schema["paths"][path][method]["parameters"]
            if parameter["in"] == "path"
        }
        assert "user_ref" in parameters
        assert "username" in parameters["user_ref"]["description"]
        assert "user_id" not in parameters
        assert "orcid" not in parameters

    assert "/users/{user_id}" not in schema["paths"]


def test_deprecated_key_verify_matches_admin_key_status_for_both_date_formats(client, db_engine, auth_as):
    from datetime import datetime, timedelta, timezone
    from app.models.core_models import ApiToken
    _setup(db_engine)
    recent = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    with Session(db_engine) as session:
        session.add(ApiToken(user_id=internal_id(session, PERSON_ORCID), user_unique_id=PERSON_ORCID, token_created=recent, token="k1", access_group_id="g"))
        session.add(ApiToken(user_id=internal_id(session, OTHER_ORCID), user_unique_id=OTHER_ORCID, token_created="2024-01-02", token="k2", access_group_id="g"))
        session.commit()
    auth_as("admin", platform_role=PlatformRole.ADMIN)

    for orcid, valid in ((PERSON_ORCID, True), (OTHER_ORCID, False)):
        verify = client.get(f"/users/{orcid}/apikey/verify")
        assert verify.status_code == 200, verify.text
        assert verify.headers["deprecation"].startswith("@")
        assert verify.json()["valid"] is valid
        status = client.get(f"/users/{orcid}").json()["api_key_status"]
        assert verify.json() == status
    assert client.get(f"/users/{SERVICE_ACCOUNT_MFID}/apikey/verify").status_code == 404

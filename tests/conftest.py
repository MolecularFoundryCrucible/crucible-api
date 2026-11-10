import atexit
import json
import os
import tempfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

TEST_SERVICE_ACCOUNT = "crucible-test@test-project.iam.gserviceaccount.com"
_TEST_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
# Application Default Credentials from a throwaway key file, so signing runs for real without network
# access, and a developer's own Google credentials never reach a test.
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as _key_file:
    json.dump({"type": "service_account", "project_id": "test-project", "private_key_id": "test", "private_key": _TEST_KEY,
               "client_email": TEST_SERVICE_ACCOUNT, "client_id": "1", "token_uri": "https://oauth2.googleapis.com/token"}, _key_file)
atexit.register(os.unlink, _key_file.name)
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = _key_file.name
# An empty environment file, so a developer's own ./.env (real database and API credentials) never
# reaches a test.
with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as _env_file:
    pass
atexit.register(os.unlink, _env_file.name)
os.environ["DB_CONF_FILE"] = _env_file.name
os.environ.pop("MULTIPART_UPLOAD_SIGNER", None)

for _name, _value in {
    "DB_SERVER": "localhost",
    "DB_USERNAME": "test",
    "DB_DATABASE": "test",
    "PGDB_PW": "test-only",
    "CRUCIBLE_SECRET_KEY": "test-only",
    "RABBITMQ_DEFAULT_PW": "test-only",
    "API_PUBLIC_URL": "https://127.0.0.1:7777",
    "API_PATH": "testapi",
    "UPLOAD_BUCKET": "mf-storage-prod",
    "GCS_PROJECT": "crucible-prod",
    "INGESTION_ENABLED": "true",
    "RMQ_HOST": "localhost",
    "RMQ_USER": "test",
    "RMQ_ROUTING_SUFFIX": "test",
    "PRINTING_ENABLED": "true",
    "MQTT_HOST": "localhost",
    "MQTT_USERNAME": "test",
    "MQTT_PASSWORD": "test-only",
    "PROPOSAL_SYNC_ENABLED": "true",
    "PROPOSAL_DB_URL": "https://proposals.example.org/lookup",
    "PROPDB_APIKEY": "test-only",
}.items():
    os.environ.setdefault(_name, _value)

import pytest
from sqlmodel import SQLModel, create_engine
from sqlalchemy import text
from sqlalchemy.pool import StaticPool

from app.api.main import app
from app.tables import (
    User, AccessGroup, UserAccessGroupLink, ResourceACL, Resource, Dataset,
    AssociatedFile, IngestionRequest, Project, Sample,
    DeletionRequest, Instrument, AccessGroupJoinRequest, DatasetSampleLink, SampleLink,
    DatasetLink, ApiToken, UploadSession, DeletionAuditLog, DatasetKeywordLink, Thumbnail,
    ResourceEmbedding, UserPreferences, UserRecentResource, PostprocessingRun, FileVersion, UserIdentity, AuthEvent, ApiKey, PasswordToken, LocalCredential,
)
from app.utils.auth_utils import authenticate_user
from app.utils.permissions import GroupMembership, PlatformRole, Principal, Role
import app.utils.gcs_record_utils as gcs_record_utils
from fastapi.testclient import TestClient

TEST_TABLES = [
    User.__table__, AccessGroup.__table__, UserAccessGroupLink.__table__,
    ResourceACL.__table__, Resource.__table__, Dataset.__table__,
    AssociatedFile.__table__, IngestionRequest.__table__, Project.__table__,
    Sample.__table__, DeletionRequest.__table__,
    Instrument.__table__, AccessGroupJoinRequest.__table__, DatasetSampleLink.__table__,
    SampleLink.__table__, DatasetLink.__table__, ApiToken.__table__, UploadSession.__table__,
    DeletionAuditLog.__table__, DatasetKeywordLink.__table__, Thumbnail.__table__,
    ResourceEmbedding.__table__, UserPreferences.__table__, UserRecentResource.__table__, UserIdentity.__table__, AuthEvent.__table__, ApiKey.__table__,
    PostprocessingRun.__table__, FileVersion.__table__, PasswordToken.__table__, LocalCredential.__table__,
]


@pytest.fixture
def use_engine(monkeypatch):
    """Point get_engine() at a test engine for this test."""
    from app.api import dependencies

    def use(engine):
        monkeypatch.setattr(dependencies, "_setup_engine", lambda env_file=None: engine)
        dependencies.get_engine.cache_clear()

    yield use
    dependencies.get_engine.cache_clear()


@pytest.fixture
def db_engine(use_engine):
    """Fresh in-memory SQLite DB per test, returned by get_engine()."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine, tables=TEST_TABLES)
    # scientific_metadata_new isn't in TEST_TABLES: its scientific_metadata
    # column is JSONB (postgres-only), which SQLite can't compile via
    # create_all. samp.resource.scientific_metadata / ds.resource... are
    # accessed unconditionally by several read routes, so the table must
    # still exist for the lazy load to succeed even when unused.
    with engine.connect() as conn:
        conn.execute(text(
            "CREATE TABLE scientific_metadata_new "
            "(id INTEGER PRIMARY KEY, unique_id VARCHAR, scientific_metadata TEXT)"
        ))
        conn.commit()
    use_engine(engine)
    yield engine


@pytest.fixture
def orcid_provider_settings(monkeypatch):
    def configure(issuer="https://orcid.org", client_id="APP-TEST"):
        from app import config

        monkeypatch.setenv("CRUCIBLE_AUTH_PROVIDERS", json.dumps([{
            "key": "orcid", "display_name": "ORCID", "issuer": issuer, "client_id": client_id, "scopes": "openid",
            "token_endpoint_auth_method": "client_secret_post", "registration": "open", "legacy_callback": True,
        }]))
        monkeypatch.setenv("OIDC_ORCID_CLIENT_SECRET", "test-orcid-secret")
        config.get_settings.cache_clear()
    return configure


@pytest.fixture
def hub_client_registered(monkeypatch):
    """Register the Hub as the one sign-in client, so its sessions are accepted."""
    from app import config
    from tests.hub_clients import HUB_TEST_CALLBACK

    monkeypatch.setenv("HUB_AUTH_ENABLED", "true")
    monkeypatch.setenv("HUB_AUTH_CALLBACK_URI", HUB_TEST_CALLBACK)
    monkeypatch.setenv("HUB_AUTH_CLIENT_SECRET", "test-hub-secret-that-is-long-enough-0123")
    config.get_settings.cache_clear()


@pytest.fixture(autouse=True)
def fresh_settings(monkeypatch):
    """Settings are read once per process in production; tests change the environment, so each
    test starts from an empty settings cache, no secrets already fetched, and no engine."""
    from app import config, storage
    from app.api import dependencies
    from app.auth import providers
    from app.storage import gcs

    caches = (config.get_settings, dependencies.get_engine, storage.get_store, gcs.server_credentials, gcs.server_client)
    monkeypatch.setattr(config, "_fetched_secrets", {})
    monkeypatch.setattr(config, "_loaded_env_files", set())
    for cached in caches:
        cached.cache_clear()
    providers.forget_clients()
    yield
    for cached in caches:
        cached.cache_clear()
    providers.forget_clients()


@pytest.fixture(autouse=True)
def block_record_bucket(monkeypatch):
    def _refuse():
        raise RuntimeError("tests must not write to the record bucket")
    monkeypatch.setattr(gcs_record_utils, "get_store", _refuse)


@pytest.fixture
def client(db_engine):
    return TestClient(app)


@pytest.fixture
def auth_as():
    """Override authorize_user for the duration of a test.

    Usage: auth_as("0000-0000-0000-0001", groups=[("proj_x", 5)])
           auth_as("0000-...-0002", groups=[("proj_x", 5, Role.EDITOR)])
           auth_as("0000-...-0003", platform_role=PlatformRole.ADMIN)

    A group may be given as (name, id) or (name, id, role); the standing
    defaults to owner so existing tests keep the access they had under the
    pre-role model.
    """
    def _set(user_id, groups=None, platform_role=PlatformRole.NONE,
             is_service_account=False, credential="api_key", authenticated_at=None):
        if groups is None:
            groups = [("test_group", 1)]
        memberships = tuple(
            GroupMembership(id=g[1], group_name=g[0],
                            role=g[2] if len(g) > 2 else Role.OWNER)
            for g in groups
        )
        principal = Principal(
            user_id=user_id,
            platform_role=platform_role,
            is_service_account=is_service_account,
            groups=memberships,
            credential=credential,
            authenticated_at=authenticated_at,
        )
        def current():
            from dataclasses import replace

            from sqlmodel import Session, select

            from app.api.dependencies import get_engine
            from app.tables import User

            try:
                with Session(get_engine()) as session:
                    internal_user_id = session.exec(select(User.id).where(User.unique_id == user_id)).first()
            except Exception:
                internal_user_id = None
            return replace(principal, internal_user_id=internal_user_id)

        app.dependency_overrides[authenticate_user] = current

    yield _set
    app.dependency_overrides.pop(authenticate_user, None)

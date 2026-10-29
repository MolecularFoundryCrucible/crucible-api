import os

for _name, _value in {
    "DB_SERVER": "localhost",
    "DB_USERNAME": "test",
    "DB_DATABASE": "test",
    "PGDB_PW": "test-only",
    "CRUCIBLE_SECRET_KEY": "test-only",
    "RABBITMQ_DEFAULT_PW": "test-only",
    "API_CONF_FILE": "app/conf/api_conf_local.yaml",
    "UPLOAD_BUCKET": "mf-storage-prod",
    "GCS_PROJECT": "crucible-prod",
    "GCS_SA": "test-only",
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
from app.models.core_models import (
    User, AccessGroup, UserAccessGroupLink, ResourceACL, Resource, Dataset,
    AssociatedFile, IngestionRequest, Project, Sample,
    DeletionRequest, Instrument, AccessGroupJoinRequest, DatasetSampleLink, SampleLink,
    DatasetLink, ApiToken, UploadSession, DeletionAuditLog, DatasetKeywordLink, Thumbnail,
    ResourceEmbedding, UserPreferences, UserRecentResource, PostprocessingRun,
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
    ResourceEmbedding.__table__, UserPreferences.__table__, UserRecentResource.__table__,
    PostprocessingRun.__table__,
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


@pytest.fixture(autouse=True)
def fresh_settings(monkeypatch):
    """Settings are read once per process in production; tests change the environment, so each
    test starts from an empty settings cache, no secrets already fetched, and no engine."""
    from app import config
    from app.api import dependencies

    monkeypatch.setattr(config, "_fetched_secrets", {})
    monkeypatch.setattr(config, "_loaded_env_files", set())
    config.get_settings.cache_clear()
    dependencies.get_engine.cache_clear()
    yield
    config.get_settings.cache_clear()
    dependencies.get_engine.cache_clear()


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
             is_service_account=False):
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
        )
        app.dependency_overrides[authenticate_user] = lambda: principal

    yield _set
    app.dependency_overrides.pop(authenticate_user, None)

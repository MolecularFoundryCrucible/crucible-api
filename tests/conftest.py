import pytest
from sqlmodel import SQLModel, create_engine
from sqlalchemy.pool import StaticPool

from app.api.main import app
from app.models.core_models import (
    User, AccessGroup, UserAccessGroupLink, ResourceACL, Resource, Dataset,
    AssociatedFile, IngestionRequest,
)
from app.utils.auth_utils import authorize_user
import app.api.routes.files as files_routes
import app.api.routes.datasets as datasets_routes
from fastapi.testclient import TestClient

TEST_TABLES = [
    User.__table__, AccessGroup.__table__, UserAccessGroupLink.__table__,
    ResourceACL.__table__, Resource.__table__, Dataset.__table__,
    AssociatedFile.__table__, IngestionRequest.__table__,
]


@pytest.fixture
def db_engine():
    """Fresh in-memory SQLite DB per test, wired into the route modules under test."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine, tables=TEST_TABLES)
    files_routes.engine = engine
    datasets_routes.engine = engine
    yield engine


@pytest.fixture
def client(db_engine):
    return TestClient(app)


class _FakeAccessGroup:
    def __init__(self, group_name, id):
        self.group_name = group_name
        self.id = id


@pytest.fixture
def auth_as():
    """Override authorize_user for the duration of a test.

    Usage: auth_as("0000-0000-0000-0001", groups=[("proj_x", 5)])
    """
    def _set(user_id, groups=None):
        groups = groups or [("test_group", 1)]
        fake_groups = [_FakeAccessGroup(name, gid) for name, gid in groups]
        app.dependency_overrides[authorize_user] = lambda: (user_id, fake_groups)

    yield _set
    app.dependency_overrides.pop(authorize_user, None)

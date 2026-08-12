import pytest
from sqlmodel import SQLModel, create_engine
from sqlalchemy.pool import StaticPool

from app.api.main import app
from app.models.core_models import (
    User, AccessGroup, UserAccessGroupLink, ResourceACL, Resource, Dataset,
    AssociatedFile, IngestionRequest, Project, UserProjectLink, Sample,
    DeletionRequest, Instrument,
)
from app.utils.auth_utils import authorize_user
from app.utils.permissions import GroupMembership, PlatformRole, Principal, Role
import app.api.routes.files as files_routes
import app.api.routes.datasets as datasets_routes
import app.api.routes.instruments as instruments_routes
import app.api.routes.projects as projects_routes
import app.api.routes.resources as resources_routes
import app.api.routes.samples as samples_routes
import app.api.routes.users as users_routes
from fastapi.testclient import TestClient

TEST_TABLES = [
    User.__table__, AccessGroup.__table__, UserAccessGroupLink.__table__,
    ResourceACL.__table__, Resource.__table__, Dataset.__table__,
    AssociatedFile.__table__, IngestionRequest.__table__, Project.__table__,
    UserProjectLink.__table__, Sample.__table__, DeletionRequest.__table__,
    Instrument.__table__,
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
    instruments_routes.engine = engine
    projects_routes.engine = engine
    resources_routes.engine = engine
    samples_routes.engine = engine
    users_routes.engine = engine
    yield engine


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
        groups = groups or [("test_group", 1)]
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
        app.dependency_overrides[authorize_user] = lambda: principal

    yield _set
    app.dependency_overrides.pop(authorize_user, None)

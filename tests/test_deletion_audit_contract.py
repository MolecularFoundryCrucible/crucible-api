import pytest
from fastapi import HTTPException
from sqlmodel import Session

import app.api.routes.deletion as deletion_routes
from app.api.main import app
from app.models.core_models import DeletionAuditLog, User
from app.models.deletion import DeletableResourceType, DeletionAuditScope
from app.models.query_params import SortDirection
from app.utils.permissions import PlatformRole, Principal


def _seed_audit_entries(db_engine):
    with Session(db_engine) as session:
        session.add(User(
            unique_id="requester",
            username="requester-user",
            first_name="Request",
            last_name="Owner",
            email="requester@example.com",
        ))
        session.add(User(
            unique_id="reviewer",
            username="reviewer-user",
            first_name="Review",
            last_name="Admin",
            email="reviewer@example.com",
        ))
        for index in range(1, 4):
            session.add(DeletionAuditLog(
                resource_id=f"resource-{index}",
                resource_type="dataset",
                resource_name=f"Resource {index}",
                project_id="project-slug",
                project_mfid="project-mfid",
                requester_id="requester",
                reviewer_id="reviewer",
                deleted_at="2026-09-23T08:00:00+00:00",
            ))
        session.commit()


def _admin():
    return Principal(user_id="platform-admin", platform_role=PlatformRole.ADMIN)


def _list_audit(auth_res, **overrides):
    params = {
        "scope": DeletionAuditScope.ALL,
        "resource_id": None,
        "resource_type": None,
        "project_id": None,
        "project_mfid": None,
        "requester_id": None,
        "reviewer_id": None,
        "direction": SortDirection.DESC,
        "limit": 100,
        "cursor": None,
        "offset": 0,
    }
    params.update(overrides)
    return deletion_routes.list_deletion_audit(auth_res=auth_res, **params)


def test_deletion_audit_cursor_is_stable_and_embeds_public_identities(db_engine):
    _seed_audit_entries(db_engine)

    first = _list_audit(_admin(), limit=2)

    assert [item.resource_id for item in first.items] == [
        "resource-3",
        "resource-2",
    ]
    assert first.total == 3
    assert first.offset == 0
    assert first.next_cursor
    assert first.items[0].requester.model_dump() == {
        "unique_id": "requester",
        "username": "requester-user",
        "first_name": "Request",
        "last_name": "Owner",
        "is_service_account": False,
        "user_capabilities": None,
    }
    assert first.items[0].reviewer.unique_id == "reviewer"
    assert not hasattr(first.items[0].requester, "email")

    second = _list_audit(
        _admin(),
        limit=2,
        cursor=first.next_cursor,
    )

    assert [item.resource_id for item in second.items] == ["resource-1"]
    assert second.offset is None
    assert second.next_cursor is None


def test_deletion_audit_filters_and_direction_are_bound_to_cursor(db_engine):
    _seed_audit_entries(db_engine)

    ascending = _list_audit(
        _admin(),
        project_mfid="project-mfid",
        resource_type=DeletableResourceType.DATASET,
        direction=SortDirection.ASC,
        limit=1,
    )

    assert ascending.items[0].resource_id == "resource-1"
    assert ascending.items[0].project_mfid == "project-mfid"

    with pytest.raises(HTTPException) as exc:
        _list_audit(
            _admin(),
            project_mfid="another-project",
            resource_type=DeletableResourceType.DATASET,
            direction=SortDirection.ASC,
            limit=1,
            cursor=ascending.next_cursor,
        )

    assert exc.value.status_code == 422
    assert exc.value.detail == "Invalid cursor"


def test_deletion_audit_detail_embeds_public_identities(db_engine):
    _seed_audit_entries(db_engine)

    response = deletion_routes.get_deletion_audit(auth_res=_admin(), audit_id=1)

    assert response.requester.unique_id == "requester"
    assert response.reviewer.unique_id == "reviewer"


def test_deletion_audit_remains_platform_admin_only(db_engine):
    _seed_audit_entries(db_engine)

    with pytest.raises(HTTPException) as exc:
        _list_audit(Principal(user_id="ordinary-user"))

    assert exc.value.status_code == 403


def test_human_requester_can_list_and_read_submitted_deletion_history(db_engine):
    _seed_audit_entries(db_engine)
    requester = Principal(user_id="requester")

    result = _list_audit(requester, scope=DeletionAuditScope.SUBMITTED)
    detail = deletion_routes.get_deletion_audit(auth_res=requester, audit_id=1)

    assert result.total == 3
    assert all(item.requester_id == "requester" for item in result.items)
    assert detail.requester_id == "requester"


def test_submitted_scope_rejects_another_requester_filter(db_engine):
    _seed_audit_entries(db_engine)

    with pytest.raises(HTTPException) as exc:
        _list_audit(
            Principal(user_id="requester"),
            scope=DeletionAuditScope.SUBMITTED,
            requester_id="another-user",
        )

    assert exc.value.status_code == 403


def test_service_account_cannot_read_deletion_audit(db_engine):
    _seed_audit_entries(db_engine)
    service_admin = Principal(
        user_id="service-admin",
        platform_role=PlatformRole.ADMIN,
        is_service_account=True,
    )

    with pytest.raises(HTTPException) as collection_exc:
        _list_audit(service_admin)
    with pytest.raises(HTTPException) as detail_exc:
        deletion_routes.get_deletion_audit(auth_res=service_admin, audit_id=1)

    assert collection_exc.value.status_code == 403
    assert detail_exc.value.status_code == 403


def test_deletion_audit_openapi_publishes_additive_contract():
    schema = app.openapi()
    parameters = {
        parameter["name"]
        for parameter in schema["paths"]["/deletion_audit"]["get"]["parameters"]
    }
    fields = schema["components"]["schemas"]["DeletionAuditLogRead"]["properties"]

    assert {
        "resource_type",
        "scope",
        "project_id",
        "project_mfid",
        "direction",
        "cursor",
    } <= parameters
    assert {"project_mfid", "requester", "reviewer"} <= fields.keys()

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

import app.api.routes.deletion as deletion_routes
import app.api.routes.resources as resources_routes
from app.api.main import app
from app.models.core_models import (
    AccessGroup,
    Dataset,
    DeletionRequest,
    Resource,
    ResourceACL,
)
from app.models.deletion import DeletionRequestScope
from app.utils.permissions import AccessGroupType, GroupMembership, PlatformRole, Principal, Role


def _create_resource(db_engine, permission: Role = Role.CONTRIBUTOR) -> int:
    with Session(db_engine) as session:
        group = AccessGroup(
            group_name="deletion-requester",
            group_type=AccessGroupType.IDENTITY.value,
        )
        session.add(group)
        session.add(Resource(mfid="dataset-to-delete", resource_type="dataset"))
        session.commit()
        session.refresh(group)
        session.add(ResourceACL(
            mfid="dataset-to-delete",
            accessgroup_id=group.id,
            read=True,
            write=permission >= Role.CONTRIBUTOR,
            permission=permission.db_value,
        ))
        session.commit()
        return group.id


def _principal(group_id: int) -> Principal:
    return Principal(
        user_id="requester",
        groups=(GroupMembership(
            id=group_id,
            group_name="deletion-requester",
            role=Role.OWNER,
        ),),
    )


def test_deletion_request_rejects_viewer(db_engine):
    group_id = _create_resource(db_engine, Role.VIEWER)

    with pytest.raises(HTTPException) as exc:
        resources_routes.create_deletion_request(
            _principal(group_id),
            "dataset-to-delete",
        )

    assert exc.value.status_code == 403
    with Session(db_engine) as session:
        assert session.exec(select(DeletionRequest)).all() == []


@pytest.mark.parametrize(
    "permission",
    [Role.CONTRIBUTOR, Role.EDITOR, Role.ADMIN, Role.OWNER],
)
def test_deletion_request_accepts_contributor_or_higher(
    db_engine,
    permission,
):
    group_id = _create_resource(db_engine, permission)

    response = resources_routes.create_deletion_request(
        _principal(group_id),
        "dataset-to-delete",
    )

    assert response.resource_id == "dataset-to-delete"
    assert response.status == "pending"
    assert response.capabilities.can_review is (permission >= Role.ADMIN)


def test_deletion_request_accepts_platform_admin_without_acl(
    db_engine,
):
    with Session(db_engine) as session:
        session.add(Resource(mfid="dataset-to-delete", resource_type="dataset"))
        session.commit()
    principal = Principal(
        user_id="platform-admin",
        platform_role=PlatformRole.ADMIN,
    )

    response = resources_routes.create_deletion_request(
        principal,
        "dataset-to-delete",
    )

    assert response.status == "pending"
    assert response.capabilities.can_review is True


def test_deletion_request_returns_conflict_for_existing_active_request(
    db_engine,
):
    group_id = _create_resource(db_engine)

    first = resources_routes.create_deletion_request(
        _principal(group_id),
        "dataset-to-delete",
    )
    with pytest.raises(HTTPException) as exc:
        resources_routes.create_deletion_request(
            _principal(group_id),
            "dataset-to-delete",
        )

    assert first.status == "pending"
    assert exc.value.status_code == 409
    with Session(db_engine) as session:
        assert len(session.exec(select(DeletionRequest)).all()) == 1


def test_active_deletion_request_unique_index(db_engine):
    with Session(db_engine) as session:
        session.add(Resource(mfid="dataset-to-delete", resource_type="dataset"))
        session.commit()
        session.add(DeletionRequest(
            resource_id="dataset-to-delete",
            requester_id="first",
            status="pending",
        ))
        session.commit()
        session.add(DeletionRequest(
            resource_id="dataset-to-delete",
            requester_id="second",
            status="approved",
        ))
        with pytest.raises(IntegrityError):
            session.commit()


@pytest.mark.parametrize(
    ("permission", "expected_total"),
    [
        (Role.EDITOR, 0),
        (Role.ADMIN, 1),
        (Role.OWNER, 1),
    ],
)
def test_reviewable_scope_requires_admin_or_owner(
    db_engine,
    permission,
    expected_total,
):
    group_id = _create_resource(db_engine, permission)
    with Session(db_engine) as session:
        session.add(DeletionRequest(
            resource_id="dataset-to-delete",
            requester_id="another-user",
            status="pending",
        ))
        session.commit()

    response = deletion_routes.list_deletion_requests(
        _principal(group_id),
        scope=DeletionRequestScope.REVIEWABLE,
        status=None,
        limit=100,
        offset=0,
    )

    assert response.total == expected_total
    assert len(response.items) == expected_total
    if response.items:
        assert response.items[0].capabilities.can_review is True


def test_submitted_scope_returns_only_callers_requests(db_engine):
    group_id = _create_resource(db_engine, Role.CONTRIBUTOR)
    with Session(db_engine) as session:
        session.add(DeletionRequest(
            resource_id="dataset-to-delete",
            requester_id="requester",
            status="pending",
        ))
        session.commit()

    response = deletion_routes.list_deletion_requests(
        _principal(group_id),
        scope=DeletionRequestScope.SUBMITTED,
        status=None,
        limit=100,
        offset=0,
    )

    assert response.total == 1
    assert response.items[0].requester_id == "requester"
    assert response.items[0].capabilities.can_review is False


def test_service_account_cannot_review_with_admin_acl(db_engine):
    group_id = _create_resource(db_engine, Role.ADMIN)
    with Session(db_engine) as session:
        session.add(DeletionRequest(
            resource_id="dataset-to-delete",
            requester_id="another-user",
            status="pending",
        ))
        session.commit()
    principal = Principal(
        user_id="service-account",
        is_service_account=True,
        groups=(GroupMembership(
            id=group_id,
            group_name="deletion-requester",
            role=Role.OWNER,
        ),),
    )

    response = deletion_routes.list_deletion_requests(
        principal,
        scope=DeletionRequestScope.REVIEWABLE,
        status=None,
        limit=100,
        offset=0,
    )

    assert response.total == 0
    assert response.items == []


def test_requester_can_read_own_deletion_request_detail(db_engine):
    group_id = _create_resource(db_engine, Role.CONTRIBUTOR)
    with Session(db_engine) as session:
        request = DeletionRequest(
            resource_id="dataset-to-delete",
            requester_id="requester",
            status="pending",
        )
        session.add(request)
        session.commit()
        session.refresh(request)
        request_id = request.id

    response = deletion_routes.get_deletion_request(
        auth_res=_principal(group_id),
        request_id=request_id,
    )

    assert response.id == request_id
    assert response.capabilities.can_review is False


def test_non_requester_without_admin_cannot_read_deletion_request_detail(db_engine):
    group_id = _create_resource(db_engine, Role.EDITOR)
    with Session(db_engine) as session:
        request = DeletionRequest(
            resource_id="dataset-to-delete",
            requester_id="another-user",
            status="pending",
        )
        session.add(request)
        session.commit()
        session.refresh(request)
        request_id = request.id

    with pytest.raises(HTTPException) as exc:
        deletion_routes.get_deletion_request(
            auth_res=_principal(group_id),
            request_id=request_id,
        )

    assert exc.value.status_code == 403


def test_project_filter_returns_canonical_project_reference(db_engine):
    with Session(db_engine) as session:
        for suffix in ("one", "two"):
            resource_id = f"dataset-{suffix}"
            session.add_all([
                Dataset(
                    unique_id=resource_id,
                    dataset_name=suffix,
                    project_id=f"project-{suffix}",
                    project_mfid=f"project-mfid-{suffix}",
                ),
                Resource(mfid=resource_id, resource_type="dataset"),
                DeletionRequest(
                    resource_id=resource_id,
                    requester_id="requester",
                    status="pending",
                ),
            ])
        session.commit()

    response = deletion_routes.list_deletion_requests(
        Principal(user_id="platform-admin", platform_role=PlatformRole.ADMIN),
        scope=DeletionRequestScope.ACCESSIBLE,
        status=None,
        project_mfid="project-mfid-two",
        limit=100,
        offset=0,
    )

    assert response.total == 1
    assert response.items[0].resource_id == "dataset-two"
    assert response.items[0].project_id == "project-two"
    assert response.items[0].project_mfid == "project-mfid-two"


def test_deletion_request_order_uses_id_as_timestamp_tiebreaker(db_engine):
    request_time = "2026-09-22T12:00:00Z"
    with Session(db_engine) as session:
        for suffix in ("one", "two"):
            resource_id = f"dataset-{suffix}"
            session.add_all([
                Dataset(unique_id=resource_id, dataset_name=suffix),
                Resource(mfid=resource_id, resource_type="dataset"),
                DeletionRequest(
                    resource_id=resource_id,
                    requester_id="requester",
                    status="pending",
                    request_time=request_time,
                ),
            ])
        session.commit()

    response = deletion_routes.list_deletion_requests(
        Principal(user_id="platform-admin", platform_role=PlatformRole.ADMIN),
        scope=DeletionRequestScope.ACCESSIBLE,
        status=None,
        limit=100,
        offset=0,
    )

    assert [item.resource_id for item in response.items] == [
        "dataset-two",
        "dataset-one",
    ]


def test_deletion_request_openapi_publishes_scope_and_capabilities():
    app.openapi_schema = None
    schema = app.openapi()
    parameters = schema["paths"]["/deletion_requests"]["get"]["parameters"]
    scope = next(parameter for parameter in parameters if parameter["name"] == "scope")
    scope_schema = schema["components"]["schemas"]["DeletionRequestScope"]
    request_schema = schema["components"]["schemas"]["DeletionRequestRead"]
    capability_schema = schema["components"]["schemas"]["DeletionRequestCapabilities"]
    status_schema = schema["components"]["schemas"]["DeletionRequestStatus"]
    decision_schema = schema["components"]["schemas"]["DeletionReviewDecision"]
    review_parameters = schema["paths"]["/deletion_requests/{request_id}"]["patch"]["parameters"]
    review_status = next(
        parameter for parameter in review_parameters if parameter["name"] == "status"
    )

    assert scope["schema"]["$ref"] == "#/components/schemas/DeletionRequestScope"
    assert scope_schema["enum"] == ["accessible", "submitted", "reviewable"]
    assert "capabilities" in request_schema["required"]
    assert "can_review" in capability_schema["required"]
    assert "project_mfid" in request_schema["properties"]
    assert status_schema["enum"] == ["pending", "approved", "rejected"]
    assert decision_schema["enum"] == ["approved", "rejected"]
    assert review_status["schema"]["$ref"] == "#/components/schemas/DeletionReviewDecision"
    assert any(parameter["name"] == "project_mfid" for parameter in parameters)

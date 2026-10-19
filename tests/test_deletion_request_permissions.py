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
    Project,
    Resource,
    ResourceACL,
    User,
)
from app.models.deletion import DeletionRequestScope, DeletionRequestSort
from app.models.query_params import SortDirection
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
    assert response.resource_mfid == "dataset-to-delete"
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


def test_deletion_request_responses_embed_public_actor_summaries(db_engine):
    group_id = _create_resource(db_engine, Role.ADMIN)
    with Session(db_engine) as session:
        session.add_all([
            User(
                unique_id="requester",
                username="requester-user",
                first_name="Request",
                last_name="User",
                email="requester@example.org",
                is_service_account=False,
            ),
            User(
                unique_id="reviewer",
                username="review-service",
                first_name="Review",
                last_name="Service",
                email="reviewer@example.org",
                is_service_account=True,
            ),
        ])
        request = DeletionRequest(
            resource_id="dataset-to-delete",
            requester_id="requester",
            reviewer_id="reviewer",
            status="rejected",
        )
        session.add(request)
        session.commit()
        session.refresh(request)
        request_id = request.id

    response = deletion_routes.list_deletion_requests(
        _principal(group_id),
        scope=DeletionRequestScope.ACCESSIBLE,
        status=None,
        limit=100,
        offset=0,
    )
    detail = deletion_routes.get_deletion_request(
        auth_res=_principal(group_id),
        request_id=request_id,
    )

    for item in (response.items[0], detail):
        assert item.requester.unique_id == "requester"
        assert item.requester.username == "requester-user"
        assert item.requester.model_dump().get("email") is None
        assert item.reviewer.unique_id == "reviewer"
        assert item.reviewer.is_service_account is True


def test_deletion_request_actor_summaries_are_null_when_unresolved(db_engine):
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
        scope=DeletionRequestScope.ACCESSIBLE,
        status=None,
        limit=100,
        offset=0,
    )

    assert response.items[0].requester is None
    assert response.items[0].reviewer is None


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
                Project(unique_id=f"project-mfid-{suffix}", project_id=f"project-{suffix}", organization="MF", project_lead_orcid="requester"),
                Dataset(
                    unique_id=resource_id,
                    dataset_name=suffix,
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

    ascending = deletion_routes.list_deletion_requests(
        Principal(user_id="platform-admin", platform_role=PlatformRole.ADMIN),
        scope=DeletionRequestScope.ACCESSIBLE,
        status=None,
        sort=DeletionRequestSort.REQUEST_TIME,
        direction=SortDirection.ASC,
        limit=100,
        offset=0,
    )

    assert [item.resource_id for item in ascending.items] == [
        "dataset-one",
        "dataset-two",
    ]


def test_deletion_request_cursor_is_stable_when_earlier_requests_change(db_engine):
    with Session(db_engine) as session:
        for index in range(1, 4):
            resource_id = f"dataset-{index}"
            session.add_all([
                Dataset(unique_id=resource_id, dataset_name=resource_id),
                Resource(mfid=resource_id, resource_type="dataset"),
                DeletionRequest(
                    resource_id=resource_id,
                    requester_id="requester",
                    status="pending",
                    request_time=f"2026-09-{index:02d}T12:00:00Z",
                ),
            ])
        session.commit()

    principal = Principal(
        user_id="platform-admin",
        platform_role=PlatformRole.ADMIN,
    )
    first = deletion_routes.list_deletion_requests(
        principal,
        scope=DeletionRequestScope.ACCESSIBLE,
        status=None,
        limit=2,
        offset=0,
    )

    assert [item.resource_id for item in first.items] == ["dataset-3", "dataset-2"]
    assert first.next_cursor is not None

    with Session(db_engine) as session:
        removed = session.exec(
            select(DeletionRequest).where(
                DeletionRequest.resource_id == "dataset-3"
            )
        ).one()
        session.delete(removed)
        session.add_all([
            Dataset(unique_id="dataset-4", dataset_name="dataset-4"),
            Resource(mfid="dataset-4", resource_type="dataset"),
            DeletionRequest(
                resource_id="dataset-4",
                requester_id="requester",
                status="pending",
                request_time="2026-09-04T12:00:00Z",
            ),
        ])
        session.commit()

    second = deletion_routes.list_deletion_requests(
        principal,
        scope=DeletionRequestScope.ACCESSIBLE,
        status=None,
        limit=2,
        cursor=first.next_cursor,
        offset=100,
    )

    assert [item.resource_id for item in second.items] == ["dataset-1"]
    assert second.offset is None
    assert second.total == 3
    assert second.next_cursor is None


def test_deletion_request_cursor_is_bound_to_query_and_principal(db_engine):
    with Session(db_engine) as session:
        for index in range(1, 3):
            resource_id = f"dataset-{index}"
            session.add_all([
                Dataset(unique_id=resource_id, dataset_name=resource_id),
                Resource(mfid=resource_id, resource_type="dataset"),
                DeletionRequest(
                    resource_id=resource_id,
                    requester_id="requester",
                    status="pending",
                    request_time=f"2026-09-{index:02d}T12:00:00Z",
                ),
            ])
        session.commit()

    principal = Principal(
        user_id="platform-admin",
        platform_role=PlatformRole.ADMIN,
    )
    first = deletion_routes.list_deletion_requests(
        principal,
        scope=DeletionRequestScope.ACCESSIBLE,
        status=None,
        limit=1,
        offset=0,
    )

    with pytest.raises(HTTPException) as changed_direction:
        deletion_routes.list_deletion_requests(
            principal,
            scope=DeletionRequestScope.ACCESSIBLE,
            status=None,
            direction=SortDirection.ASC,
            limit=1,
            cursor=first.next_cursor,
            offset=0,
        )
    assert changed_direction.value.status_code == 422

    with pytest.raises(HTTPException) as changed_sort:
        deletion_routes.list_deletion_requests(
            principal,
            scope=DeletionRequestScope.ACCESSIBLE,
            status=None,
            sort=DeletionRequestSort.RESOURCE_NAME,
            limit=1,
            cursor=first.next_cursor,
            offset=0,
        )
    assert changed_sort.value.status_code == 422

    with pytest.raises(HTTPException) as changed_principal:
        deletion_routes.list_deletion_requests(
            Principal(user_id="another-admin", platform_role=PlatformRole.ADMIN),
            scope=DeletionRequestScope.ACCESSIBLE,
            status=None,
            limit=1,
            cursor=first.next_cursor,
            offset=0,
        )
    assert changed_principal.value.status_code == 422

    with pytest.raises(HTTPException) as changed_privilege_mode:
        deletion_routes.list_deletion_requests(
            Principal(user_id="platform-admin"),
            scope=DeletionRequestScope.ACCESSIBLE,
            status=None,
            limit=1,
            cursor=first.next_cursor,
            offset=0,
        )
    assert changed_privilege_mode.value.status_code == 422


def test_deletion_request_sort_modes_and_cursors(db_engine):
    users = [
        User(
            unique_id="requester-smith",
            first_name="Amy",
            last_name="Smith",
            username="smith",
            email=None,
        ),
        User(
            unique_id="requester-adams-bob",
            first_name="Bob",
            last_name="adams",
            username="bob",
            email=None,
        ),
        User(
            unique_id="requester-adams-z",
            first_name="Alice",
            last_name="Adams",
            username="zeta",
            email=None,
        ),
        User(
            unique_id="requester-adams-a",
            first_name="Alice",
            last_name="Adams",
            username="alpha",
            email=None,
        ),
    ]
    requests = [
        ("dataset-one", "alpha", "beta", "requester-smith", "rejected"),
        ("dataset-two", "Beta", "Alpha", "requester-adams-bob", "pending"),
        ("dataset-three", None, None, "requester-missing", "approved"),
        ("dataset-four", "charlie", "gamma", "requester-adams-z", "pending"),
        ("dataset-five", "delta", "delta", "requester-adams-a", "approved"),
        ("dataset-six", None, None, "requester-missing-two", "pending"),
    ]
    with Session(db_engine) as session:
        session.add_all(users)
        for project_id in {request[2] for request in requests if request[2]}:
            session.add(Project(unique_id=f"mfid-{project_id}", project_id=project_id, organization="MF", project_lead_orcid="requester-smith"))
        for resource_id, resource_name, project_id, requester_id, status in requests:
            session.add_all([
                Resource(mfid=resource_id, resource_type="dataset"),
                Dataset(
                    unique_id=resource_id,
                    dataset_name=resource_id,
                    project_mfid=f"mfid-{project_id}" if project_id else None,
                ),
                DeletionRequest(
                    resource_id=resource_id,
                    resource_name=resource_name,
                    requester_id=requester_id,
                    status=status,
                ),
            ])
        session.commit()

    expected = {
        (DeletionRequestSort.RESOURCE_NAME, SortDirection.ASC): [
            "dataset-one",
            "dataset-two",
            "dataset-four",
            "dataset-five",
            "dataset-three",
            "dataset-six",
        ],
        (DeletionRequestSort.RESOURCE_NAME, SortDirection.DESC): [
            "dataset-five",
            "dataset-four",
            "dataset-two",
            "dataset-one",
            "dataset-six",
            "dataset-three",
        ],
        (DeletionRequestSort.PROJECT_ID, SortDirection.ASC): [
            "dataset-two",
            "dataset-one",
            "dataset-five",
            "dataset-four",
            "dataset-three",
            "dataset-six",
        ],
        (DeletionRequestSort.PROJECT_ID, SortDirection.DESC): [
            "dataset-four",
            "dataset-five",
            "dataset-one",
            "dataset-two",
            "dataset-six",
            "dataset-three",
        ],
        (DeletionRequestSort.REQUESTER_NAME, SortDirection.ASC): [
            "dataset-five",
            "dataset-four",
            "dataset-two",
            "dataset-one",
            "dataset-three",
            "dataset-six",
        ],
        (DeletionRequestSort.REQUESTER_NAME, SortDirection.DESC): [
            "dataset-one",
            "dataset-two",
            "dataset-four",
            "dataset-five",
            "dataset-six",
            "dataset-three",
        ],
        (DeletionRequestSort.STATUS, SortDirection.ASC): [
            "dataset-two",
            "dataset-four",
            "dataset-six",
            "dataset-three",
            "dataset-five",
            "dataset-one",
        ],
        (DeletionRequestSort.STATUS, SortDirection.DESC): [
            "dataset-one",
            "dataset-five",
            "dataset-three",
            "dataset-six",
            "dataset-four",
            "dataset-two",
        ],
    }
    principal = Principal(user_id="platform-admin", platform_role=PlatformRole.ADMIN)

    for (sort, direction), expected_ids in expected.items():
        actual_ids = []
        cursor = None
        while True:
            response = deletion_routes.list_deletion_requests(
                principal,
                scope=DeletionRequestScope.ACCESSIBLE,
                status=None,
                sort=sort,
                direction=direction,
                limit=1,
                cursor=cursor,
                offset=0,
            )
            actual_ids.extend(item.resource_id for item in response.items)
            cursor = response.next_cursor
            if cursor is None:
                break
        assert actual_ids == expected_ids


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
    sort_schema = schema["components"]["schemas"]["DeletionRequestSort"]
    review_parameters = schema["paths"]["/deletion_requests/{request_id}"]["patch"]["parameters"]
    review_status = next(
        parameter for parameter in review_parameters if parameter["name"] == "status"
    )

    assert scope["schema"]["$ref"] == "#/components/schemas/DeletionRequestScope"
    assert scope_schema["enum"] == ["accessible", "submitted", "reviewable"]
    assert "capabilities" in request_schema["required"]
    assert "can_review" in capability_schema["required"]
    assert "project_mfid" in request_schema["properties"]
    assert "requester" in request_schema["required"]
    assert "reviewer" in request_schema["required"]
    assert status_schema["enum"] == ["pending", "approved", "rejected"]
    assert decision_schema["enum"] == ["approved", "rejected"]
    assert sort_schema["enum"] == [
        "request_time",
        "resource_name",
        "project_id",
        "requester_name",
        "status",
    ]
    assert review_status["schema"]["$ref"] == "#/components/schemas/DeletionReviewDecision"
    assert any(parameter["name"] == "project_mfid" for parameter in parameters)
    assert any(parameter["name"] == "direction" for parameter in parameters)
    assert any(parameter["name"] == "cursor" for parameter in parameters)

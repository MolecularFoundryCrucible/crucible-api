import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

import app.api.routes.resources as resources_routes
from app.models.core_models import AccessGroup, DeletionRequest, Resource, ResourceACL
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

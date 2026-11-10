import pytest
from fastapi import HTTPException
from sqlmodel import Session, select

import app.api.routes.deletion as deletion_routes
import app.utils.deletion_utils as deletion_utils
from app.tables import (
    AccessGroup,
    Dataset,
    DeletionAuditLog,
    DeletionRequest,
    Resource,
    ResourceACL,
    ResourceEmbedding,
    Sample,
    User,
    UserRecentResource,
)
from app.utils.permissions import AccessGroupType, GroupMembership, PlatformRole, Role
from tests.people import person_principal, user_id_or_none


def _setup(
    db_engine,
    permission=Role.OWNER,
    resource_type="dataset",
    request=False,
    embedding=False,
):
    with Session(db_engine) as session:
        group = session.exec(
            select(AccessGroup).where(AccessGroup.group_name == "resource-owner")
        ).one_or_none()
        if group is None:
            group = AccessGroup(group_name="resource-owner", group_type=AccessGroupType.IDENTITY.value)
            session.add(group)
        resource = Resource(mfid="resource-to-delete", resource_type=resource_type)
        session.add(resource)
        if resource_type == "dataset":
            session.add(Dataset(unique_id=resource.mfid, dataset_name="delete me"))
        elif resource_type == "sample":
            session.add(Sample(unique_id=resource.mfid, sample_name="delete me"))
        if embedding:
            session.add(ResourceEmbedding(
                resource_mfid=resource.mfid,
                chunk_index=0,
                chunk_text="embedded text",
                embedding=[0.0] * 768,
                model_name="test-model",
            ))
        session.commit()
        session.refresh(group)
        session.add(ResourceACL(
            mfid=resource.mfid,
            accessgroup_id=group.id,
            read=True,
            write=True,
            permission=permission.db_value,
        ))
        deletion_request = None
        if request:
            deletion_request = DeletionRequest(
                resource_id=resource.mfid,
                resource_name="delete me",
                requester_id="requester",
                requester_user_id=user_id_or_none(session, "requester"),
                status="pending",
            )
            session.add(deletion_request)
        session.commit()
        if deletion_request is not None:
            session.refresh(deletion_request)
        return group.id, deletion_request.id if deletion_request else None


def _principal(group_id):
    return person_principal(
        user_id="actor",
        groups=(GroupMembership(group_id, "resource-owner", Role.OWNER),),
    )


def test_approval_deletes_database_rows_and_writes_audit_atomically(db_engine):
    group_id, request_id = _setup(db_engine, permission=Role.ADMIN, request=True)

    response = deletion_routes.review_deletion_request(
        auth_res=_principal(group_id),
        request_id=request_id,
        status="approved",
    )

    assert response.status == "approved"
    with Session(db_engine) as session:
        assert session.get(Resource, "resource-to-delete") is None
        assert session.exec(select(Dataset).where(Dataset.unique_id == "resource-to-delete")).one_or_none() is None
        assert session.exec(select(DeletionRequest)).all() == []
        audit = session.exec(select(DeletionAuditLog)).one()
        assert audit.resource_id == "resource-to-delete"


def test_approval_rolls_back_audit_and_status_when_deletion_fails(db_engine, monkeypatch):
    group_id, request_id = _setup(db_engine, permission=Role.ADMIN, request=True)

    def fail_delete(*args, **kwargs):
        raise RuntimeError("delete failed")

    monkeypatch.setattr(deletion_utils, "hard_delete_resource", fail_delete)

    with pytest.raises(RuntimeError, match="delete failed"):
        deletion_routes.review_deletion_request(
            auth_res=_principal(group_id),
            request_id=request_id,
            status="approved",
        )

    with Session(db_engine) as session:
        assert session.get(Resource, "resource-to-delete") is not None
        assert session.get(DeletionRequest, request_id).status == "pending"
        assert session.exec(select(DeletionAuditLog)).all() == []


def test_forced_deletion_requires_owner_not_admin(db_engine):
    group_id, _ = _setup(db_engine, permission=Role.ADMIN)

    with pytest.raises(HTTPException) as exc:
        deletion_routes.hard_delete_resource_route(
            resource_id="resource-to-delete",
            background_tasks=None,
            auth_res=_principal(group_id),
            force=True,
        )

    assert exc.value.status_code == 403


def test_forced_deletion_requires_explicit_true_flag(db_engine):
    group_id, _ = _setup(db_engine)

    with pytest.raises(HTTPException) as exc:
        deletion_routes.hard_delete_resource_route(
            resource_id="resource-to-delete",
            background_tasks=None,
            auth_res=_principal(group_id),
            force=False,
        )

    assert exc.value.status_code == 422


def test_owner_and_platform_admin_can_force_delete(db_engine):
    group_id, _ = _setup(db_engine)
    result = deletion_routes.hard_delete_resource_route(
        resource_id="resource-to-delete",
        background_tasks=None,
        auth_res=_principal(group_id),
        force=True,
    )
    assert "permanently deleted" in result["detail"]

    group_id, _ = _setup(db_engine)
    result = deletion_routes.hard_delete_resource_route(
        resource_id="resource-to-delete",
        background_tasks=None,
        auth_res=person_principal(user_id="platform-admin", platform_role=PlatformRole.ADMIN),
        force=True,
    )
    assert "permanently deleted" in result["detail"]


def test_forced_deletion_rejects_unsupported_resource_before_audit(db_engine):
    group_id, _ = _setup(db_engine, resource_type="project")

    with pytest.raises(HTTPException) as exc:
        deletion_routes.hard_delete_resource_route(
            resource_id="resource-to-delete",
            background_tasks=None,
            auth_res=_principal(group_id),
            force=True,
        )

    assert exc.value.status_code == 400
    with Session(db_engine) as session:
        assert session.get(Resource, "resource-to-delete") is not None
        assert session.exec(select(DeletionAuditLog)).all() == []


def test_forced_deletion_supports_samples(db_engine):
    group_id, _ = _setup(db_engine, resource_type="sample")

    deletion_routes.hard_delete_resource_route(
        resource_id="resource-to-delete",
        background_tasks=None,
        auth_res=_principal(group_id),
        force=True,
    )

    with Session(db_engine) as session:
        assert session.get(Resource, "resource-to-delete") is None
        assert session.exec(select(Sample).where(Sample.unique_id == "resource-to-delete")).one_or_none() is None


@pytest.mark.parametrize("resource_type", ["dataset", "sample"])
def test_forced_deletion_removes_resource_embeddings(db_engine, resource_type):
    group_id, _ = _setup(db_engine, resource_type=resource_type, embedding=True)

    deletion_routes.hard_delete_resource_route(
        resource_id="resource-to-delete",
        background_tasks=None,
        auth_res=_principal(group_id),
        force=True,
    )

    with Session(db_engine) as session:
        assert session.exec(select(ResourceEmbedding)).all() == []


@pytest.mark.parametrize("resource_type", ["dataset", "sample"])
def test_forced_deletion_removes_recent_resource_entries(db_engine, resource_type):
    from app.general_utils import get_utc_datetime
    group_id, _ = _setup(db_engine, resource_type=resource_type)
    with Session(db_engine) as session:
        user = User(first_name="Recent", last_name="Viewer", email=None, unique_id="0000-0003-0000-0001", username="recent")
        session.add(user)
        session.commit()
        session.add(UserRecentResource(user_id=user.id, resource_mfid="resource-to-delete", viewed_at=get_utc_datetime()))
        session.commit()

    deletion_routes.hard_delete_resource_route(
        resource_id="resource-to-delete",
        background_tasks=None,
        auth_res=_principal(group_id),
        force=True,
    )

    with Session(db_engine) as session:
        assert session.exec(select(UserRecentResource)).all() == []


def test_forced_deletion_removes_postprocessing_runs(db_engine):
    from app.general_utils import get_utc_datetime
    from app.tables import PostprocessingRun
    group_id, _ = _setup(db_engine)
    with Session(db_engine) as session:
        session.add(PostprocessingRun(dataset_mfid="resource-to-delete", pipeline="rga_analysis", requested_at=get_utc_datetime()))
        session.commit()

    deletion_routes.hard_delete_resource_route(
        resource_id="resource-to-delete",
        background_tasks=None,
        auth_res=_principal(group_id),
        force=True,
    )

    with Session(db_engine) as session:
        assert session.exec(select(PostprocessingRun)).all() == []

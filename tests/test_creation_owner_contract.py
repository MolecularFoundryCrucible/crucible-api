
import pytest
from fastapi import BackgroundTasks
from pydantic import ValidationError
from sqlmodel import Session, select

from app.api.routes.datasets import create_dataset
from app.api.routes.samples import create_sample
from app.models.core_models import AccessGroup, Dataset, ResourceACL, Sample, User, UserAccessGroupLink
from app.models.datasets import DatasetCreate
from app.models.samples import SampleCreate
from app.utils.permissions import (
    AccessGroupType, PlatformRole, Principal, PUBLIC_GROUP_NAME, Role)


SERVICE_ACCOUNT_ID = "0tkn2knjast3h0008nyq9zps2c"


def _add_identity(session, unique_id, username=None, is_service_account=False):
    user = User(
        first_name="Test",
        last_name="Owner",
        email=f"{unique_id}@example.org",
        unique_id=unique_id,
        username=username,
        is_service_account=is_service_account,
    )
    group = AccessGroup(group_name=unique_id, group_type=AccessGroupType.IDENTITY.value)
    session.add(user)
    session.add(group)
    session.flush()
    session.add(UserAccessGroupLink(user_id=user.id, accessgroup_id=group.id, role="owner"))
    session.commit()
    session.refresh(user)
    session.refresh(group)
    return user, group


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (DatasetCreate, {"owner": "owner", "owner_orcid": "0000-0001"}),
        (SampleCreate, {"sample_name": "sample", "owner": "owner", "owner_orcid": "0000-0001"}),
        (DatasetCreate, {"owner_user_id": 1}),
        (SampleCreate, {"sample_name": "sample", "owner_user_id": 1}),
    ],
)
def test_create_owner_schema_rejects_ambiguous_or_internal_fields(model, payload):
    with pytest.raises(ValidationError):
        model.model_validate(payload)


@pytest.mark.parametrize("model", [DatasetCreate, SampleCreate])
def test_owner_orcid_is_deprecated_in_create_schema(model):
    schema = model.model_json_schema()
    assert schema["properties"]["owner_orcid"]["deprecated"] is True


def test_service_account_defaults_to_sample_owner_and_uses_generated_mfid_for_public_acl(db_engine):
    with Session(db_engine) as session:
        owner, owner_group = _add_identity(session, SERVICE_ACCOUNT_ID, is_service_account=True)
        public_group = AccessGroup(group_name=PUBLIC_GROUP_NAME, group_type=AccessGroupType.SYSTEM.value)
        session.add(public_group)
        session.commit()
        session.refresh(public_group)
        owner_id = owner.id
        owner_group_id = owner_group.id
        public_group_id = public_group.id

    response = create_sample(
        Principal(user_id=SERVICE_ACCOUNT_ID, is_service_account=True),
        SampleCreate(sample_name="service-account-owned", public=True),
        BackgroundTasks(),
    )

    assert response.owner_orcid == SERVICE_ACCOUNT_ID
    with Session(db_engine) as session:
        sample = session.exec(select(Sample).where(Sample.unique_id == response.unique_id)).one()
        assert sample.owner_user_id == owner_id
        owner_acl = session.exec(
            select(ResourceACL)
            .where(ResourceACL.mfid == response.unique_id)
            .where(ResourceACL.accessgroup_id == owner_group_id)
        ).one()
        public_acl = session.exec(
            select(ResourceACL)
            .where(ResourceACL.mfid == response.unique_id)
            .where(ResourceACL.accessgroup_id == public_group_id)
        ).one()
        assert owner_acl.permission == Role.OWNER.db_value
        assert public_acl.permission == Role.VIEWER.db_value


def test_dataset_owner_reference_resolves_to_canonical_identity(db_engine):
    human_id = "0000-0001-6402-3752"
    with Session(db_engine) as session:
        owner, owner_group = _add_identity(session, human_id, username="dataset-owner")
        owner_id = owner.id
        owner_group_id = owner_group.id

    response = create_dataset(
        Principal(
            user_id=SERVICE_ACCOUNT_ID,
            platform_role=PlatformRole.CONTRIBUTOR,
            is_service_account=True,
        ),
        DatasetCreate(dataset_name="owned-by-reference", owner="dataset-owner"),
        BackgroundTasks(),
    )

    assert response.owner_orcid == human_id
    with Session(db_engine) as session:
        dataset = session.exec(select(Dataset).where(Dataset.unique_id == response.unique_id)).one()
        assert dataset.owner_user_id == owner_id
        owner_acl = session.exec(
            select(ResourceACL)
            .where(ResourceACL.mfid == response.unique_id)
            .where(ResourceACL.accessgroup_id == owner_group_id)
        ).one()
        assert owner_acl.permission == Role.OWNER.db_value

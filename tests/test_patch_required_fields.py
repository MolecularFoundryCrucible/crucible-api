import pytest
from pydantic import ValidationError
from sqlmodel import Session

from app.tables import Resource, Sample
from app.models.datasets import AssociatedFileUpdate
from app.models.projects import ProjectUpdate
from app.models.samples import SampleUpdate
from app.models.users import AccountProfileUpdate, UserUpdate
from app.utils.permissions import PlatformRole

REQUIRED = [
    (SampleUpdate, "sample_name", "S1"),
    (ProjectUpdate, "project_id", "proj-1"),
    (ProjectUpdate, "organization", "LBL"),
    (AccountProfileUpdate, "first_name", "Ada"),
    (AccountProfileUpdate, "last_name", "Lovelace"),
    (UserUpdate, "first_name", "Ada"),
    (UserUpdate, "last_name", "Lovelace"),
    (AssociatedFileUpdate, "filename", "a.h5"),
    (AssociatedFileUpdate, "storage_backend", "globus"),
    (AssociatedFileUpdate, "size", 3),
    (AssociatedFileUpdate, "sha256_hash", "a" * 64),
]
NULLABLE = [
    (SampleUpdate, "description"),
    (ProjectUpdate, "title"),
    (AccountProfileUpdate, "email"),
    (UserUpdate, "username"),
    (AssociatedFileUpdate, "access_note"),
]


@pytest.mark.parametrize("model, field, value", REQUIRED)
def test_required_fields_can_be_omitted_or_set_but_not_nulled(model, field, value):
    assert field not in model.model_validate({}).model_dump(exclude_unset=True)
    assert model.model_validate({field: value}).model_dump(exclude_unset=True) == {field: value}
    with pytest.raises(ValidationError):
        model.model_validate({field: None})


@pytest.mark.parametrize("model, field", NULLABLE)
def test_nullable_fields_can_still_be_cleared(model, field):
    assert model.model_validate({field: None}).model_dump(exclude_unset=True) == {field: None}


@pytest.mark.parametrize("model, field", [(SampleUpdate, "sample_name"), (ProjectUpdate, "organization"), (UserUpdate, "first_name"), (AccountProfileUpdate, "last_name")])
def test_names_cannot_be_blank(model, field):
    with pytest.raises(ValidationError):
        model.model_validate({field: "   "})
    assert model.model_validate({field: "  Ada "}).model_dump(exclude_unset=True) == {field: "Ada"}


@pytest.mark.parametrize("schema, field", [(m.__name__, f) for m, f, _ in REQUIRED])
def test_openapi_does_not_advertise_null_for_required_fields(client, schema, field):
    prop = client.get("/openapi.json").json()["components"]["schemas"][schema]["properties"][field]
    assert "anyOf" not in prop and prop.get("type") != "null" and "default" not in prop


def test_patching_a_sample_name_to_null_is_rejected_and_leaves_it_unchanged(client, db_engine, auth_as):
    with Session(db_engine) as session:
        session.add_all([Sample(unique_id="s1", sample_name="Original"), Resource(mfid="s1", resource_type="sample")])
        session.commit()
    auth_as("admin", platform_role=PlatformRole.ADMIN)

    assert client.patch("/samples/s1", json={"sample_name": None}).status_code == 422
    assert client.patch("/samples/s1", json={"description": "kept name"}).status_code == 200
    assert client.get("/samples/s1").json()["sample_name"] == "Original"

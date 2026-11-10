"""Table models read and write their fields under the field names, whatever the sqlmodel version.

Newer sqlmodel passes a Field `alias` to Pydantic, which would rename these fields in every
response and drop values validated by field name.
"""
import pytest

from app.tables import IngestionRequest, Resource, Sample


@pytest.mark.parametrize("model, field", [
    (IngestionRequest, "dataset_mfid"),
    (Sample, "timestamp"),
    (Resource, "mfid"),
    (Resource, "resource_type"),
])
def test_fields_keep_their_names(model, field):
    assert model.model_fields[field].alias is None
    assert field in model(**{field: "x"}).model_dump(by_alias=True)


def test_ingestion_request_validates_by_field_name():
    assert IngestionRequest.model_validate({"dataset_mfid": "ds"}).dataset_mfid == "ds"

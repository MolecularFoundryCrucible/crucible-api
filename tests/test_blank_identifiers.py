"""Empty-string identifiers must normalise to None on the way in.

`""` and `None` mean the same thing for an optional identifier — "not provided"
— but only `None` behaves that way. An empty string is a real value: it matches
a real row, satisfies a foreign key, and creates a real access group.

Confirmed uploaders send `project_id=""` rather than omitting it, which on
production produced a nameless project holding 1,292 datasets and 3 samples,
plus an access group named `""`. See ~/crucible-audits/empty-project-datasets/.
"""
import pytest
from pydantic import ValidationError

from app.general_utils import blank_to_none
from app.models.datasets import DatasetCreate, DatasetUpdate
from app.models.samples import SampleCreate, SampleUpdate

# DatasetUpdate/SampleUpdate no longer accept project_id or owner_orcid at
# all (POST /resources/{mfid}/project and .../transfer_ownership instead),
# so the blank-coercion behavior below only applies to Create.
# SampleCreate requires sample_name; DatasetCreate takes no required fields.
MODELS = [
    (DatasetCreate, {}),
    (SampleCreate, {"sample_name": "s1"}),
]


class TestBlankToNone:
    @pytest.mark.parametrize("value", ["", " ", "\t", "\n", "   \t\n  "])
    def test_blank_becomes_none(self, value):
        assert blank_to_none(value) is None

    @pytest.mark.parametrize("value", ["MFP00001", "0", "a", " padded "])
    def test_real_values_pass_through_unchanged(self, value):
        assert blank_to_none(value) == value

    def test_none_stays_none(self):
        assert blank_to_none(None) is None

    def test_non_strings_are_untouched(self):
        for value in (0, False, [], {}):
            assert blank_to_none(value) is value


@pytest.mark.parametrize("model,required", MODELS)
class TestModelsCoerceBlankProjectId:
    def test_empty_project_id_becomes_none(self, model, required):
        assert model(**required, project_id="").project_id is None

    def test_whitespace_project_id_becomes_none(self, model, required):
        assert model(**required, project_id="   ").project_id is None

    def test_real_project_id_survives(self, model, required):
        assert model(**required, project_id="MFP00001").project_id == "MFP00001"

    def test_omitted_project_id_is_none(self, model, required):
        assert model(**required).project_id is None

    def test_empty_owner_orcid_becomes_none(self, model, required):
        if "owner_orcid" not in model.model_fields:
            pytest.skip(f"{model.__name__} has no owner_orcid")
        assert model(**required, owner_orcid="").owner_orcid is None


@pytest.mark.parametrize("field", ["instrument_id", "instrument_mfid"])
def test_dataset_create_coerces_blank_instrument_identifiers(field):
    assert getattr(DatasetCreate(**{field: "  "}), field) is None


@pytest.mark.parametrize("model,required", MODELS)
def test_create_models_coerce_blank_project_mfid(model, required):
    assert model(**required, project_mfid="  ").project_mfid is None


@pytest.mark.parametrize("model,required", MODELS)
def test_create_models_reject_invalid_project_mfid(model, required):
    with pytest.raises(ValidationError):
        model(**required, project_mfid="not-an-mfid")


@pytest.mark.parametrize("model", [DatasetCreate, SampleCreate])
def test_create_model_schemas_expose_optional_project_mfid(model):
    schema = model.model_json_schema()
    assert "project_mfid" in schema["properties"]
    assert "project_mfid" not in schema.get("required", [])


def test_dataset_create_rejects_invalid_instrument_mfid():
    with pytest.raises(ValidationError):
        DatasetCreate(instrument_mfid="not-an-mfid")


def test_dataset_create_schema_exposes_optional_instrument_mfid():
    schema = DatasetCreate.model_json_schema()
    assert "instrument_mfid" in schema["properties"]
    assert "instrument_mfid" not in schema.get("required", [])


class TestUpdateModelsRejectReassignmentFields:
    def test_dataset_update_rejects_project_id(self):
        with pytest.raises(ValidationError):
            DatasetUpdate(project_id="MFP00001")

    def test_dataset_update_rejects_owner_orcid(self):
        with pytest.raises(ValidationError):
            DatasetUpdate(owner_orcid="0000-0001-2345-6789")

    def test_sample_update_rejects_project_id(self):
        with pytest.raises(ValidationError):
            SampleUpdate(project_id="MFP00001")


def test_the_specific_payload_that_caused_this():
    """An instrument omitting the project but sending the key anyway."""
    payload = {
        "unique_id": "0tkmxbj3x5vg1000bv8kjxyykr",
        "dataset_name": "260806_131324_picam_readout.h5",
        "project_id": "",
        "instrument_name": "",
    }
    ds = DatasetCreate(**payload)
    assert ds.project_id is None, (
        "an empty project_id must not reach the database — it matches a real "
        "project row and pulls the dataset into a nameless project"
    )

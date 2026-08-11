"""Empty-string identifiers must normalise to None on the way in.

`""` and `None` mean the same thing for an optional identifier — "not provided"
— but only `None` behaves that way. An empty string is a real value: it matches
a real row, satisfies a foreign key, and creates a real access group.

Confirmed uploaders send `project_id=""` rather than omitting it, which on
production produced a nameless project holding 1,292 datasets and 3 samples,
plus an access group named `""`. See ~/crucible-audits/empty-project-datasets/.
"""
import pytest

from app.general_utils import blank_to_none
from app.models.datasets import DatasetCreate, DatasetUpdate
from app.models.samples import SampleCreate, SampleUpdate

# SampleCreate requires sample_name; the others take no required fields.
MODELS = [
    (DatasetCreate, {}),
    (DatasetUpdate, {}),
    (SampleCreate, {"sample_name": "s1"}),
    (SampleUpdate, {}),
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


def test_the_specific_payload_that_caused_this():
    """An instrument omitting the project but sending the key anyway."""
    payload = {
        "unique_id": "0tkmxbj3x5vg1000bv8kjxyykr",
        "dataset_name": "260806_131324_picam_readout.h5",
        "project_id": "",
        "instrument_name": "HiP_Microscope",
    }
    ds = DatasetCreate(**payload)
    assert ds.project_id is None, (
        "an empty project_id must not reach the database — it matches a real "
        "project row and pulls the dataset into a nameless project"
    )

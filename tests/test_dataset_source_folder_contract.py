from types import SimpleNamespace

from app.models.datasets import DatasetCreate, DatasetRead, DatasetResponse
from app.utils.gcs_record_utils import dataset_record


def test_source_folder_is_not_in_dataset_api_schemas():
    for schema in (DatasetCreate, DatasetRead, DatasetResponse):
        assert "source_folder" not in schema.model_json_schema()["properties"]


def test_source_folder_is_not_serialized_in_new_dataset_records():
    dataset = SimpleNamespace(
        unique_id="0tmfkc1dk4r84000xjz5j6pqr5",
        dataset_name="dataset",
        owner_orcid="0000-0001-2345-6789",
        project_mfid="0tmfkc1dk4r84000xjz5j6pqr6",
        instrument_mfid="0tmfkc1dk4r84000xjz5j6pqr7",
        measurement="measurement",
        data_type="data-type",
        data_format="format",
        session_name="session",
        source_folder="deprecated/path",
        timestamp="2026-08-27T00:00:00Z",
        size=10,
        public=False,
    )

    record = dataset_record(dataset, None, False)

    assert "source_folder" not in record

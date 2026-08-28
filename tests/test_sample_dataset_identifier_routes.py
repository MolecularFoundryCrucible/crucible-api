from sqlmodel import Session

from app.models.core_models import Dataset, Sample


SAMPLE_MFID = "0tmfkc1dk4r84000xjz5j6pqr4"
DATASET_MFID = "0tmfkc1dk4r84000xjz5j6pqr5"


def _setup(db_engine):
    with Session(db_engine) as session:
        session.add(Sample(unique_id=SAMPLE_MFID, sample_name="human-sample-name"))
        session.add(Dataset(unique_id=DATASET_MFID, dataset_name="human-dataset-name"))
        session.commit()


def test_sample_name_is_not_a_singleton_identifier(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as("0000-0001-2345-6789")

    response = client.get("/samples/human-sample-name")

    assert response.status_code == 404


def test_dataset_name_is_not_a_singleton_identifier(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as("0000-0001-2345-6789")

    response = client.get("/datasets/human-dataset-name")

    assert response.status_code == 404


def test_singleton_openapi_parameters_are_named_as_mfids(client):
    schema = client.get("/openapi.json").json()

    sample_parameters = schema["paths"]["/samples/{sample_mfid}"]["get"]["parameters"]
    dataset_parameters = schema["paths"]["/datasets/{dataset_mfid}"]["get"]["parameters"]

    assert any(parameter["name"] == "sample_mfid" for parameter in sample_parameters)
    assert any(parameter["name"] == "dataset_mfid" for parameter in dataset_parameters)

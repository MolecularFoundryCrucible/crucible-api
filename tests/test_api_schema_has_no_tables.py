import app.models.core_models as tables
from app.api.main import app


def test_no_route_exposes_a_database_table_as_its_request_or_response_model():
    table_names = {name for name, model in vars(tables).items() if getattr(model, "__table__", None) is not None}
    assert len(table_names) > 30
    assert sorted(table_names & set(app.openapi()["components"]["schemas"])) == []


def test_the_reads_that_replaced_tables_keep_what_clients_use():
    schemas = app.openapi()["components"]["schemas"]
    assert set(schemas["IngestionRequestRead"]["properties"]) == {"id", "dataset_mfid", "file_mfid", "file_to_upload", "ingestion_githash", "ingestion_class", "time_created", "time_completed", "status"}
    assert schemas["IngestionRequestRead"]["properties"]["id"]["type"] == "integer"
    assert set(schemas["KeywordRead"]["properties"]) == {"keyword", "num_datasets"}
    assert set(schemas["ResourceAccessGroupRead"]["properties"]) == {"mfid", "accessgroup_id", "read", "write", "permission"}

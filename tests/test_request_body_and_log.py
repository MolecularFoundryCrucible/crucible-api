import pytest
from sqlmodel import Session, select

from app.tables import AccessGroup, Dataset, Resource, ResourceACL, ScientificMetadataBinary
from app.utils.permissions import Role


def _request_lines(caplog):
    return [record.getMessage() for record in caplog.records if record.name == "app.api.main" and not hasattr(record, "audit")]


def test_request_log_names_the_authenticated_caller(client, auth_as, caplog):
    auth_as("0000-0005-0000-0001")
    with caplog.at_level("INFO", logger="app.api.main"):
        client.get("/account/preferences")
    line = next(line for line in _request_lines(caplog) if line.startswith("GET /account/preferences "))
    assert line.endswith(" actor=0000-0005-0000-0001 credential=api_key")


def test_request_log_marks_anonymous_requests(client, caplog):
    with caplog.at_level("INFO", logger="app.api.main"):
        client.get("/openapi.json")
    line = next(line for line in _request_lines(caplog) if line.startswith("GET /openapi.json "))
    assert line.endswith(" actor=- credential=-")


@pytest.fixture
def dataset(client, db_engine, auth_as):
    with Session(db_engine) as session:
        session.add(AccessGroup(id=1, group_name="test_group"))
        session.add(Resource(mfid="ds-nul", resource_type="dataset"))
        session.add(Dataset(unique_id="ds-nul", dataset_name="CL"))
        session.add(ResourceACL(mfid="ds-nul", accessgroup_id=1, read=True, write=True, permission=Role.OWNER.db_value))
        session.commit()
    auth_as("0000-0005-0000-0002", groups=[("test_group", 1, Role.OWNER)])
    return client


def stored(db_engine):
    with Session(db_engine) as session:
        return session.exec(select(ScientificMetadataBinary).where(ScientificMetadataBinary.unique_id == "ds-nul")).one().scientific_metadata


NUL_METADATA = {"H39_Obsolete": "\u0000\u0000", "Name": "scan1", "nested": {"pad\u0000": ["a\u0000", 3, None]}}


def test_metadata_with_nul_characters_is_rejected_with_paths(dataset, db_engine):
    response = dataset.post("/resources/ds-nul/metadata", json=NUL_METADATA)
    assert response.status_code == 422, response.text
    assert [error["loc"] for error in response.json()["detail"]] == [
        ["body", "H39_Obsolete"],
        ["body", "nested", "pad\\u0000"],
        ["body", "nested", "pad\\u0000", 0],
    ]
    assert all(error["type"] == "unsupported_character" for error in response.json()["detail"])
    with Session(db_engine) as session:
        assert session.exec(select(ScientificMetadataBinary)).all() == []


def test_escaped_backslash_u0000_text_is_allowed(dataset, db_engine):
    response = dataset.post("/resources/ds-nul/metadata", content='{"note": "literal \\\\u0000 text"}', headers={"Content-Type": "application/json"})
    assert response.status_code == 200, response.text
    assert stored(db_engine) == {"note": "literal \\u0000 text"}


def test_nul_rejected_on_any_json_route(client, auth_as):
    auth_as("0000-0005-0000-0003")
    response = client.patch("/account/preferences", json={"hub": {"note": "a\u0000"}})
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "hub", "note"]
    assert response.json()["detail"][0]["type"] == "unsupported_character"


@pytest.mark.parametrize("body, location, kind", [
    ('{"a": {"b": "x\\ud800y"}}', ["body", "a", "b"], "unsupported_character"),
    ('{"k\\udfff": 1}', ["body", "k\\udfff"], "unsupported_character"),
    ('{"a": "\\ufdd0"}', ["body", "a"], "unsupported_character"),
    ('{"a": ["\\uffff"]}', ["body", "a", 0], "unsupported_character"),
    ('{"t": NaN}', ["body", "t"], "invalid_number"),
    ('{"t": [1, Infinity]}', ["body", "t", 1], "invalid_number"),
    ('{"t": {"u": -Infinity}}', ["body", "t", "u"], "invalid_number"),
])
def test_unstorable_json_is_rejected(dataset, db_engine, body, location, kind):
    response = dataset.post("/resources/ds-nul/metadata", content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 422, response.text
    assert response.json()["detail"][0]["loc"] == location
    assert response.json()["detail"][0]["type"] == kind
    with Session(db_engine) as session:
        assert session.exec(select(ScientificMetadataBinary)).all() == []


def test_valid_unicode_and_numbers_are_accepted(dataset, db_engine):
    body = '{"emoji": "\\ud83d\\ude00", "cjk": "結晶", "ctrl": "tab\\there\\u0001", "big": 12345678901234567890, "f": 1.5e300}'
    response = dataset.post("/resources/ds-nul/metadata", content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 200, response.text
    assert stored(db_engine)["emoji"] == "\U0001F600"


@pytest.mark.parametrize("body", ["[" * 100_000 + "]" * 100_000, '{"a":' * 100_000 + "1" + "}" * 100_000], ids=["arrays", "objects"])
def test_absurdly_nested_json_is_422_not_500(dataset, db_engine, body):
    response = dataset.post("/resources/ds-nul/metadata", content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 422, response.text
    assert response.json()["detail"][0]["type"] == "json_too_deep"


@pytest.mark.parametrize("text, reason", [
    ("plain ascii", None),
    ("ascii with \x00 nul", "NUL"),
    ("température \x00 then ﷐", "NUL"),
    ("température ﷐ then \x00", "noncharacters"),
    ("\ud800 lone surrogate", "surrogates"),
    ("emoji \U0001F600 and \U0010fffe", "noncharacters"),
])
def test_the_first_offending_character_is_reported(text, reason):
    from app.api.main import _unsupported_reason
    result = _unsupported_reason(text)
    assert result is None if reason is None else reason in result


def test_large_ascii_bodies_are_scanned_quickly():
    import time
    from app.api.main import _unsupported_reason
    text = "spectrum value 1.234e-05, " * 200_000
    started = time.perf_counter()
    assert _unsupported_reason(text) is None
    assert time.perf_counter() - started < 0.05


def test_request_log_names_the_client_of_a_deprecated_route(client, auth_as, caplog):
    auth_as("0000-0005-0000-0001")
    with caplog.at_level("INFO", logger="app.api.main"):
        client.post("/datasets/ds-none/upload/complete", json={"upload_id": "missing"}, headers={"User-Agent": 'nano-crucible/3.2.1 "x" forged=1'})
    line = next(line for line in _request_lines(caplog) if line.startswith("POST /datasets/{dataset_mfid}/upload/complete "))
    assert line.endswith(' credential=api_key user_agent="nano-crucible/3.2.1 \\"x\\" forged=1"')


def test_request_log_omits_the_client_of_a_current_route(client, auth_as, caplog):
    auth_as("0000-0005-0000-0001")
    with caplog.at_level("INFO", logger="app.api.main"):
        client.get("/account/preferences", headers={"User-Agent": "nano-crucible/3.3.0"})
    line = next(line for line in _request_lines(caplog) if line.startswith("GET /account/preferences "))
    assert "user_agent=" not in line

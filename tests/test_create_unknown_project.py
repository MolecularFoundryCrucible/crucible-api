import pytest
from sqlmodel import Session

from app.models.core_models import User

CALLER = "0000-0006-0000-0001"
MFID = "0tnq3dqdsnsrd000497vvkk650"


@pytest.fixture
def caller(client, db_engine, auth_as):
    with Session(db_engine) as session:
        session.add(User(first_name="Ed", last_name="Wong", email=None, unique_id=CALLER, username="edw"))
        session.commit()
    auth_as(CALLER)
    return client


@pytest.mark.parametrize("route, body", [
    ("/datasets", {"dataset_name": "d"}),
    ("/samples", {"sample_name": "s"}),
])
def test_mfid_in_project_id_explains_project_mfid(caller, route, body):
    response = caller.post(route, json=body | {"project_id": MFID})
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == f"Project '{MFID}' not found. project_id takes the project slug; pass an MFID as project_mfid."


@pytest.mark.parametrize("route, body", [
    ("/datasets", {"dataset_name": "d"}),
    ("/samples", {"sample_name": "s"}),
])
def test_unknown_slug_is_404_not_403(caller, route, body):
    response = caller.post(route, json=body | {"project_id": "noSuchProject"})
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Project 'noSuchProject' not found"

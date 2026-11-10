import pytest
from sqlmodel import Session

from app.tables import Dataset, Resource, ResourceACL
from tests.test_join_request_workflow import ALICE, CONTRIBUTOR, EDITOR, OWNER, STRANGER, act, world  # noqa: F401


@pytest.fixture
def films_dataset(world, db_engine):  # noqa: F811
    group_id, project_mfid = world["films"]
    with Session(db_engine) as session:
        session.add_all([Resource(mfid="ds-films", resource_type="dataset"), Dataset(unique_id="ds-films", dataset_name="d", project_mfid=project_mfid)])
        session.add(ResourceACL(mfid="ds-films", accessgroup_id=group_id, read=True, write=True, permission="admin"))
        session.commit()
    return "ds-films"


@pytest.mark.parametrize("caller", [OWNER, ALICE, EDITOR, CONTRIBUTOR, STRANGER])
def test_dataset_flag_matches_the_request_route(act, films_dataset, caller):  # noqa: F811
    detail = act(caller).get(f"/datasets/{films_dataset}")
    flag = detail.json()["capabilities"]["can_request_deletion"] if detail.status_code == 200 else False
    accepted = act(caller).post(f"/resources/{films_dataset}/delete", params={"reason": "x"}).status_code == 200
    assert flag == accepted
    assert accepted == (caller in (OWNER, EDITOR, CONTRIBUTOR))
    if accepted:
        pending = act(OWNER).get(f"/datasets/{films_dataset}").json()["capabilities"]
        assert pending["can_request_deletion"] is False
        assert act(OWNER).post(f"/resources/{films_dataset}/delete", params={"reason": "again"}).status_code == 409


@pytest.mark.parametrize("assigned", [False, True])
@pytest.mark.parametrize("caller", [OWNER, EDITOR, CONTRIBUTOR])
def test_project_flag_matches_the_request_route(act, world, films_dataset, caller, assigned):  # noqa: F811
    slug = "films" if assigned else "bulk"
    _, mfid = world[slug]
    flag = act(caller).get(f"/projects/{slug}").json()["capabilities"]["can_request_deletion"]
    accepted = act(caller).post(f"/resources/{mfid}/delete", params={"reason": "x"}).status_code == 200
    assert flag == accepted
    assert accepted == (caller == OWNER and not assigned)
    if accepted:
        assert act(OWNER).get(f"/projects/{slug}").json()["capabilities"]["can_request_deletion"] is False

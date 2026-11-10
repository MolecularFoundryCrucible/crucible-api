from sqlmodel import Session

from app.tables import Dataset, Resource, ResourceACL
from tests.test_join_request_workflow import OWNER, act, world  # noqa: F401


def test_project_deletion_requests_carry_their_own_project(act, world, db_engine):  # noqa: F811
    films_group, films = world["films"]
    _, bulk = world["bulk"]
    with Session(db_engine) as session:
        session.add_all([Dataset(unique_id="ds-films", dataset_name="d", project_mfid=films), Resource(mfid="ds-films", resource_type="dataset")])
        session.add(ResourceACL(mfid="ds-films", accessgroup_id=films_group, read=True, write=True, permission="admin"))
        session.commit()
    created = act(OWNER).post(f"/resources/{bulk}/delete", json={"reason": "empty"})
    assert created.status_code == 200, created.text
    assert act(OWNER).post("/resources/ds-films/delete", json={"reason": "old"}).status_code == 200

    listed = {item["resource_id"]: item for item in act(OWNER).get("/deletion_requests").json()["items"]}
    assert (listed[bulk]["project_id"], listed[bulk]["project_mfid"]) == ("bulk", bulk) == (created.json()["project_id"], created.json()["project_mfid"])
    assert (listed["ds-films"]["project_id"], listed["ds-films"]["project_mfid"]) == ("films", films)

    for mfid, expected in ((bulk, [bulk]), (films, ["ds-films"])):
        page = act(OWNER).get("/deletion_requests", params={"project_mfid": mfid}).json()
        assert [item["resource_id"] for item in page["items"]] == expected
        assert page["total"] == len(expected)

    ordered = act(OWNER).get("/deletion_requests", params={"sort": "project_id", "direction": "asc"}).json()["items"]
    assert [item["project_id"] for item in ordered] == ["bulk", "films"]

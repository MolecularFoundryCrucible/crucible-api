import pytest
from sqlmodel import Session, select

from app.tables import AccessGroup, Dataset, Resource, ResourceACL, Sample
from app.utils.permissions import PUBLIC_GROUP_NAME, PlatformRole, Role
from app.utils.sql_utils import is_public
from app.utils.user_utils import provision_user

OWNER = "0000-0000-0000-0001"
KINDS = {"datasets": {"dataset_name": "x"}, "samples": {"sample_name": "x"}}
RENAME = {"datasets": {"dataset_name": "y"}, "samples": {"sample_name": "y"}}


@pytest.fixture
def setup(client, auth_as, db_engine):
    auth_as(OWNER, platform_role=PlatformRole.ADMIN)
    with Session(db_engine) as session:
        provision_user(session, unique_id=OWNER, first_name="A", last_name="B", email=None, username="abc")
        session.add(AccessGroup(group_name=PUBLIC_GROUP_NAME, group_type="system"))
        session.commit()


def create(client, kind, public):
    response = client.post(f"/{kind}", json={**KINDS[kind], "public": public})
    assert response.status_code in (200, 201), response.text
    assert response.json()["public"] is public
    return response.json()["unique_id"]


def state(db_engine, mfid):
    with Session(db_engine) as session:
        grant = session.exec(select(ResourceACL).join(AccessGroup, AccessGroup.id == ResourceACL.accessgroup_id).where(ResourceACL.mfid == mfid, AccessGroup.group_name == PUBLIC_GROUP_NAME)).one_or_none()
        return grant is not None


@pytest.fixture(params=sorted(KINDS))
def kind(request):
    return request.param


def test_created_public_unpublished_then_edited_stays_private(client, setup, db_engine, kind):
    mfid = create(client, kind, True)
    assert client.delete(f"/resources/{mfid}/access/public").status_code == 200
    response = client.patch(f"/{kind}/{mfid}", json=RENAME[kind])
    assert response.status_code == 200, response.text
    assert response.json()["public"] is False
    assert state(db_engine, mfid) is False


def test_published_through_the_access_route_then_edited_stays_public(client, setup, db_engine, kind):
    mfid = create(client, kind, False)
    assert client.put(f"/resources/{mfid}/access/public").status_code == 200
    response = client.patch(f"/{kind}/{mfid}", json=RENAME[kind])
    assert response.status_code == 200, response.text
    assert response.json()["public"] is True
    assert state(db_engine, mfid) is True


@pytest.mark.parametrize("value", [True, False, None])
def test_patch_refuses_public_and_changes_nothing(client, setup, db_engine, kind, value):
    mfid = create(client, kind, False)
    response = client.patch(f"/{kind}/{mfid}", json={**RENAME[kind], "public": value})
    assert response.status_code == 422
    assert "access/public" in response.text
    assert state(db_engine, mfid) is False
    assert client.get(f"/{kind}/{mfid}").json()[next(iter(KINDS[kind]))] == "x"


@pytest.mark.parametrize("public", [True, False])
def test_creation_sets_the_starting_state(client, setup, db_engine, kind, public):
    assert state(db_engine, create(client, kind, public)) is public


def test_creation_defaults_to_private(client, setup, db_engine, kind):
    response = client.post(f"/{kind}", json=KINDS[kind])
    assert response.json()["public"] is False
    assert state(db_engine, response.json()["unique_id"]) is False


def test_detail_and_list_read_the_grant_not_a_column(client, setup, db_engine, kind):
    mfid = create(client, kind, False)
    with Session(db_engine) as session:
        session.exec(select(Resource).where(Resource.mfid == mfid)).one().public = True
        session.commit()
    assert client.get(f"/{kind}/{mfid}").json()["public"] is False
    assert {item["unique_id"]: item["public"] for item in client.get(f"/{kind}").json()["items"]}[mfid] is False
    client.put(f"/resources/{mfid}/access/public")
    with Session(db_engine) as session:
        session.exec(select(Resource).where(Resource.mfid == mfid)).one().public = False
        session.commit()
    assert client.get(f"/{kind}/{mfid}").json()["public"] is True
    assert {item["unique_id"]: item["public"] for item in client.get(f"/{kind}").json()["items"]}[mfid] is True


def test_access_routes_keep_the_old_columns_in_step_for_older_pods(client, setup, db_engine):
    mfid = create(client, "datasets", True)
    client.delete(f"/resources/{mfid}/access/public")
    with Session(db_engine) as session:
        assert session.exec(select(Resource.public).where(Resource.mfid == mfid)).one() is False
        assert session.exec(select(Dataset.public).where(Dataset.unique_id == mfid)).one() is False
    client.put(f"/resources/{mfid}/access/public")
    with Session(db_engine) as session:
        assert session.exec(select(Resource.public).where(Resource.mfid == mfid)).one() is True
        assert session.exec(select(Dataset.public).where(Dataset.unique_id == mfid)).one() is True


def test_is_public_helper(client, setup, db_engine):
    mfid = create(client, "samples", True)
    with Session(db_engine) as session:
        assert is_public(session, mfid) is True
        assert is_public(session, "no-such-record") is False


def test_editor_cannot_publish_through_any_route(client, auth_as, setup, db_engine, kind):
    mfid = create(client, kind, False)
    with Session(db_engine) as session:
        group = AccessGroup(group_name="editors", group_type="custom")
        session.add(group)
        session.flush()
        session.add(ResourceACL(mfid=mfid, accessgroup_id=group.id, read=True, write=True, permission=Role.EDITOR.db_value))
        session.commit()
        group_id = group.id
    auth_as("0000-0000-0000-0002", groups=[("editors", group_id, Role.EDITOR)])
    assert client.patch(f"/{kind}/{mfid}", json={"public": True}).status_code == 422
    assert client.put(f"/resources/{mfid}/access/public").status_code == 403
    assert client.patch(f"/{kind}/{mfid}", json=RENAME[kind]).status_code == 200
    assert state(db_engine, mfid) is False


def test_sample_table_has_no_public_column_to_drift():
    assert "public" not in Sample.model_fields


@pytest.fixture
def stored_records(monkeypatch):
    from app.api.routes import resources

    written = []
    monkeypatch.setattr(resources, "write_record", lambda mfid, record, actor, operation: written.append((mfid, record["public"], record["resource_type"], actor, operation)))
    return written


def test_access_routes_keep_the_stored_record_accurate(client, setup, kind, stored_records):
    mfid = create(client, kind, False)
    assert client.put(f"/resources/{mfid}/access/public").status_code == 200
    assert client.delete(f"/resources/{mfid}/access/public").status_code == 200
    resource_type = kind[:-1]
    assert stored_records == [(mfid, True, resource_type, OWNER, "update"), (mfid, False, resource_type, OWNER, "update")]


def test_refused_publish_writes_no_stored_record(client, auth_as, setup, kind, stored_records):
    mfid = create(client, kind, False)
    auth_as("0000-0000-0000-0002", groups=[("nobody", 999, Role.VIEWER)])
    assert client.put(f"/resources/{mfid}/access/public").status_code in (403, 404)
    assert stored_records == []


def test_publishing_something_without_a_public_stored_value_writes_none(client, setup, db_engine, stored_records):
    with Session(db_engine) as session, session.begin():
        session.add(Resource(mfid="0tinstrument000000000000a0", resource_type="instrument"))
    client.put("/resources/0tinstrument000000000000a0/access/public")
    assert stored_records == []


@pytest.mark.parametrize("call", ["create", "edit"])
def test_public_state_is_read_once_per_write(client, setup, kind, monkeypatch, call):
    import importlib

    mfid = create(client, kind, False)
    route = importlib.import_module(f"app.api.routes.{kind}")
    calls = []
    real = route.is_public
    monkeypatch.setattr(route, "is_public", lambda session, value: calls.append(value) or real(session, value))
    response = client.post(f"/{kind}", json=KINDS[kind]) if call == "create" else client.patch(f"/{kind}/{mfid}", json=RENAME[kind])
    assert response.status_code in (200, 201), response.text
    assert len(calls) == 1
    assert response.json()["public"] is False



def test_public_filter_reads_the_public_grant(client, setup, db_engine, kind):
    public, private = create(client, kind, True), create(client, kind, False)
    with Session(db_engine) as session:
        if kind == "datasets":
            session.get(Dataset, session.exec(select(Dataset.id).where(Dataset.unique_id == public)).one()).public = False
        session.commit()

    def listed(value):
        response = client.get(f"/{kind}", params={"public": value})
        assert response.status_code == 200, response.text
        return {item["unique_id"] for item in response.json()["items"]}

    assert listed("true") == {public}
    assert listed("false") == {private}

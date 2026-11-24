import json

import pytest
from sqlmodel import Session

from app.config import get_settings
from app.tables import AccessGroup, Instrument, Resource, ResourceACL
from app.utils.permissions import Role
from tests.test_project_owner_appointment_guard import _setup as project_setup

STORES = [
    {"id": "main", "display_name": "Foundry storage", "type": "gcs", "bucket": "mf-storage"},
    {"id": "vm", "display_name": "Lab VM", "type": "s3", "bucket": "crucible", "endpoint_url": "http://vm.test:9000"},
]
LEAD = "0000-0001-1111-1111"


@pytest.fixture(autouse=True)
def two_stores(monkeypatch):
    monkeypatch.delenv("UPLOAD_BUCKET")
    monkeypatch.setenv("STORAGE_STORES", json.dumps(STORES))
    monkeypatch.setenv("STORAGE_DEFAULT_STORE", "main")
    get_settings.cache_clear()


def patch(client, url, body, if_match=None):
    headers = {"Content-Type": "application/merge-patch+json"} | ({"If-Match": if_match} if if_match else {})
    return client.patch(url, content=json.dumps(body), headers=headers)


@pytest.fixture
def project(client, db_engine, auth_as):
    group_id, mfid = project_setup(db_engine)
    auth_as(LEAD, groups=[(mfid, group_id, Role.OWNER)])
    return group_id, mfid


@pytest.fixture
def instrument(client, db_engine, auth_as):
    with Session(db_engine) as session:
        session.add_all([Instrument(unique_id="in1", instrument_id="TEM_1", instrument_name="TEM", owner=LEAD, location="B67"), Resource(mfid="in1", resource_type="instrument")])
        group = AccessGroup(group_name="in1-members")
        session.add(group)
        session.commit()
        session.add(ResourceACL(mfid="in1", accessgroup_id=group.id, read=True, write=True, permission="admin"))
        session.commit()
        group_id = group.id
    auth_as(LEAD, groups=[("in1-members", group_id, Role.ADMIN)])
    return group_id


def test_a_project_admin_sets_and_clears_the_default_store(client, project):
    url = "/projects/proj_x/settings"
    first = client.get(url)
    assert (first.status_code, first.json()["settings"], first.headers["ETag"]) == (200, {}, '"s0"')
    changed = patch(client, url, {"default_store_id": "vm"}, if_match='"s0"')
    assert changed.status_code == 200, changed.text
    assert (changed.json()["settings"], changed.headers["ETag"]) == ({"default_store_id": "vm"}, '"s1"')
    cleared = patch(client, url, {"default_store_id": None})
    assert (cleared.json()["settings"], cleared.json()["revision"]) == ({}, 2)


@pytest.mark.parametrize("body", [{"colour": 1}, {"default_store_id": "gone"}, {"default_store_id": 3}])
def test_record_settings_refuse_unknown_keys_and_stores(client, project, body):
    assert patch(client, "/projects/proj_x/settings", body).status_code == 422


def test_a_stale_if_match_is_refused(client, project):
    assert patch(client, "/projects/proj_x/settings", {"default_store_id": "vm"}).status_code == 200
    stale = patch(client, "/projects/proj_x/settings", {"default_store_id": "main"}, if_match='"s0"')
    assert (stale.status_code, stale.json()["detail"]["code"]) == (412, "etag_mismatch")


@pytest.mark.parametrize("role, can_change", [(Role.VIEWER, False), (Role.EDITOR, False), (Role.ADMIN, True)])
def test_only_admins_change_record_settings(client, project, auth_as, role, can_change):
    group_id, mfid = project
    auth_as("0000-0002-2222-2222", groups=[(mfid, group_id, role)])
    assert client.get("/projects/proj_x/settings").status_code == 200
    assert patch(client, "/projects/proj_x/settings", {"default_store_id": "vm"}).status_code == (200 if can_change else 403)


def test_an_instrument_admin_sets_the_default_store(client, instrument):
    changed = patch(client, "/instruments/TEM_1/settings", {"default_store_id": "vm"})
    assert changed.status_code == 200, changed.text
    assert client.get("/instruments/TEM_1/settings").json()["settings"] == {"default_store_id": "vm"}


def test_settings_of_an_unknown_record_are_not_found(client, project):
    assert client.get("/projects/nope/settings").status_code == 404
    assert patch(client, "/instruments/nope/settings", {}).status_code == 404

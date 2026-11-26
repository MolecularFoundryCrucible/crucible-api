from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlmodel import Session, select

from app.tables import Instrument, Resource, UploadSession, User
from app.services import multipart
from app.utils.permissions import PlatformRole, Principal

OLD = "0000-0002-1825-0097"
NEW = "0tmnyhfztdwc1000w2kspzsnh9"


def _seed(db_engine):
    with Session(db_engine) as session:
        user = User(unique_id=OLD, first_name="Ada", last_name="L", email=None, username="ada")
        session.add(user)
        session.flush()
        session.add_all([
            Instrument(unique_id="inst-owned", instrument_id="owned", instrument_name="Owned", owner=OLD, owner_user_id=user.id, location="A"),
            Instrument(unique_id="inst-unmatched", instrument_id="unmatched", instrument_name="Unmatched", owner="Foundry", location="B"),
            Resource(mfid="inst-owned", resource_type="instrument"),
            Resource(mfid="inst-unmatched", resource_type="instrument"),
            UploadSession(store_id="mf-storage-prod", upload_id="up-1", dataset_mfid="ds", object_key="k", associated_file_name="f", owner_user_id=OLD, user_id=user.id,
                          expires_at=datetime.now(timezone.utc) + timedelta(days=1), protocol=multipart.PROTOCOL, state="uploading"),
        ])
        session.commit()
        user.unique_id = NEW
        session.commit()
        return user.id


def test_instrument_owner_follows_the_person_and_unmatched_owners_show_none(client, db_engine, auth_as):
    _seed(db_engine)
    auth_as("viewer", platform_role=PlatformRole.ADMIN)
    items = {item["unique_id"]: item for item in client.get("/instruments", params={"status": "active", "include_owner": "true"}).json()["items"]}
    assert (items["inst-owned"]["owner_orcid"], items["inst-owned"]["owner"]["unique_id"]) == (NEW, NEW)
    assert (items["inst-unmatched"]["owner_orcid"], items["inst-unmatched"]["owner"]) == (None, None)
    assert client.get("/resources/inst-owned").json()["owner_orcid"] == NEW
    owned = client.get("/instruments", params={"owner_id": NEW}).json()["items"]
    assert [item["unique_id"] for item in owned] == ["inst-owned"]
    assert client.get("/instruments", params={"owner_id": OLD}).json()["items"] == []
    assert client.get("/instruments", params={"owner": "Foundry"}).json()["items"] == []
    without_owner = client.get("/instruments", params={"include_owner": "false"}).json()["items"]
    assert {item["unique_id"]: (item["owner_orcid"], item["owner"]) for item in without_owner}["inst-owned"] == (NEW, None)


def test_upload_session_owner_check_uses_the_internal_id(db_engine):
    user_id = _seed(db_engine)
    with Session(db_engine) as session:
        assert multipart.owned(session, "ds", "up-1", Principal(user_id=NEW, internal_user_id=user_id), check_access=False).upload_id == "up-1"
        for stranger in (Principal(user_id=OLD, internal_user_id=user_id + 1), Principal(user_id=OLD)):
            with pytest.raises(HTTPException) as caught:
                multipart.owned(session, "ds", "up-1", stranger, check_access=False)
            assert caught.value.status_code == 403
        assert session.exec(select(UploadSession.user_id)).one() == user_id

import importlib.util
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException
from sqlalchemy import create_engine, inspect, text
from sqlmodel import Session, select

from app.models.core_models import Instrument, Resource, UploadSession, User
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
            UploadSession(upload_id="up-1", dataset_mfid="ds", object_key="k", associated_file_name="f", owner_user_id=OLD, user_id=user.id,
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


@pytest.mark.skipif(not os.getenv("CRUCIBLE_TEST_PG_URL"), reason="requires isolated test PostgreSQL")
def test_instrument_upload_user_id_migration_fills_exact_matches_and_follows_old_style_writes():
    path = Path(__file__).resolve().parents[1] / "migrations/versions/20261105_f3a9d1c7b842_instrument_upload_user_id.py"
    spec = importlib.util.spec_from_file_location("instrument_upload_user_id_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine(os.environ["CRUCIBLE_TEST_PG_URL"])
    schema = "test_instrument_upload_user_id_" + uuid4().hex
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            run = lambda sql: connection.execute(text(sql))  # noqa: E731
            one = lambda sql: connection.execute(text(sql)).one()  # noqa: E731
            run(f'CREATE SCHEMA "{schema}"')
            run(f'SET LOCAL search_path TO "{schema}"')
            run('CREATE TABLE "user" (id serial PRIMARY KEY, orcid text UNIQUE, email text)')
            run("CREATE TABLE instrument (id serial PRIMARY KEY, owner varchar NOT NULL)")
            run("CREATE TABLE upload_sessions (id serial PRIMARY KEY, owner_user_id varchar NOT NULL)")
            run('CREATE TABLE postprocessingrun (id serial PRIMARY KEY, requested_by varchar REFERENCES "user"(orcid) ON DELETE SET NULL)')
            run(f"INSERT INTO \"user\" (orcid, email) VALUES ('{OLD}', 'ada@example.com'), ('{NEW}', NULL)")
            ada, other = connection.execute(text('SELECT id FROM "user" ORDER BY id')).scalars().all()
            run(f"INSERT INTO instrument (owner) VALUES ('{OLD}'), ('undefined'), ('ada@example.com')")
            run(f"INSERT INTO upload_sessions (owner_user_id) VALUES ('{OLD}')")
            run(f"INSERT INTO postprocessingrun (requested_by) VALUES ('{OLD}')")
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                assert connection.execute(text("SELECT owner_user_id FROM instrument ORDER BY id")).scalars().all() == [ada, None, None]
                assert one("SELECT user_id FROM upload_sessions") == (ada,)
                assert one("SELECT requested_by_user_id FROM postprocessingrun") == (ada,)
                run("INSERT INTO instrument (owner) VALUES (NULL)")
                run(f"UPDATE instrument SET owner = '{NEW}' WHERE owner = 'undefined'")
                assert one(f"SELECT owner_user_id FROM instrument WHERE owner = '{NEW}'") == (other,)
                run(f"INSERT INTO upload_sessions (owner_user_id) VALUES ('{NEW}')")
                assert one(f"SELECT user_id FROM upload_sessions WHERE owner_user_id = '{NEW}'") == (other,)
                run(f"INSERT INTO postprocessingrun (requested_by) VALUES ('{NEW}')")
                assert one(f"SELECT requested_by_user_id FROM postprocessingrun WHERE requested_by = '{NEW}'") == (other,)
                migration.downgrade()
                for table, _, id_column, _, _ in migration.COLUMNS:
                    assert id_column not in {c["name"] for c in inspect(connection).get_columns(table, schema=schema)}
                assert not {c["name"]: c for c in inspect(connection).get_columns("instrument", schema=schema)}["owner"]["nullable"]
                assert one("SELECT count(*) FROM instrument WHERE owner IS NULL") == (0,)
        finally:
            transaction.rollback()
    engine.dispose()

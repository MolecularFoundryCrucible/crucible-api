import pytest
from sqlmodel import Session, select

from app.tables import AccessGroup
from tests.pg_fixture import PG_URL, throwaway_database

pytestmark = pytest.mark.skipif(not PG_URL, reason="CRUCIBLE_TEST_PG_URL not set")


def test_several_access_groups_are_written_in_one_flush():
    from app.cli.db import upgrade_database

    with throwaway_database() as engine:
        upgrade_database(engine)
        with Session(engine) as session:
            session.add_all([AccessGroup(group_name="inst-1", group_type="instrument"), AccessGroup(group_name="inst-1:members", group_type="instrument_member")])
            session.commit()
            assert sorted(session.exec(select(AccessGroup.group_type).where(AccessGroup.group_name.like("inst-1%"))).all()) == ["instrument", "instrument_member"]


def test_an_instrument_is_created_on_postgres(client, auth_as, use_engine):
    from app.cli.db import upgrade_database
    from app.tables import User
    from app.utils.user_utils import provision_user

    with throwaway_database() as engine:
        upgrade_database(engine)
        use_engine(engine)
        with Session(engine) as session:
            provision_user(session, unique_id="0000-0001-0000-0001", first_name="Ada", last_name="Lovelace", email=None, username="ada")
            session.commit()
        auth_as("0000-0001-0000-0001")
        created = client.post("/instruments", json={"instrument_id": "pg-inst", "instrument_name": "PG instrument", "location": "b30"})
        assert created.status_code == 200, created.text
        with Session(engine) as session:
            mfid = created.json()["unique_id"]
            assert sorted(session.exec(select(AccessGroup.group_type).where(AccessGroup.group_name.in_([mfid, f"{mfid}:members"]))).all()) == ["instrument", "instrument_member"]
            assert session.exec(select(User.username)).all() == ["ada"]

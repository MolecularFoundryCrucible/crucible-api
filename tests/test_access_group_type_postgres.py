import pytest
from sqlmodel import Session, select

from app.tables import AccessGroup, AccessGroupJoinRequest, User
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


def test_several_users_and_join_requests_are_written_in_one_flush():
    from app.cli.db import upgrade_database

    with throwaway_database() as engine:
        upgrade_database(engine)
        with Session(engine) as session:
            people = [User(unique_id=f"0000-0001-0000-000{n}", username=f"person{n}", first_name="P", last_name=str(n), platform_role=role) for n, role in ((1, "admin"), (2, "contributor"))]
            group = AccessGroup(group_name="proj-1", group_type="project")
            session.add_all([*people, group])
            session.commit()
            session.add_all([AccessGroupJoinRequest(accessgroup_id=group.id, requester_id=person.unique_id, requester_user_id=person.id, status="approved", granted_role=role)
                             for person, role in zip(people, ("viewer", "editor"))])
            session.commit()
            assert sorted(session.exec(select(User.platform_role)).all()) == ["admin", "contributor"]
            assert sorted(session.exec(select(AccessGroupJoinRequest.granted_role)).all()) == ["editor", "viewer"]

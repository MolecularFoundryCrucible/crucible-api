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

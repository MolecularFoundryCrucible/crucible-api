from datetime import datetime, timedelta, timezone

from sqlmodel import Session

from app.build_info import api_version
from app.tables import Resource
from app.utils.sql_utils import record_resource

FIRST = datetime(2026, 11, 1, tzinfo=timezone.utc)


def test_the_record_row_is_created_once_and_stamped_on_every_write(db_engine, monkeypatch):
    monkeypatch.setenv("GITHASH", "abc1234")
    with Session(db_engine) as session:
        record_resource(session, "sm_stamp", "sample", now=FIRST)
        session.commit()
        record_resource(session, "sm_stamp", "sample", now=FIRST + timedelta(days=1))
        session.commit()
        row = session.get(Resource, "sm_stamp")
        assert row.resource_type == "sample"
        assert row.creation_time.replace(tzinfo=timezone.utc) == FIRST
        assert row.modification_time.replace(tzinfo=timezone.utc) == FIRST + timedelta(days=1)
        assert (row.created_with_api_version, row.api_version_githash) == (f"v{api_version()}", "abc1234")

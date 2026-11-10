import importlib.util
from pathlib import Path

from sqlalchemy import text
from sqlmodel import Session

from app.tables import Instrument, Project


AUDIT_PATH = Path(__file__).parents[1] / "migrations/data/20261005_audit_case_insensitive_slugs.py"
AUDIT_SPEC = importlib.util.spec_from_file_location("audit_case_insensitive_slugs", AUDIT_PATH)
AUDIT_MODULE = importlib.util.module_from_spec(AUDIT_SPEC)
AUDIT_SPEC.loader.exec_module(AUDIT_MODULE)


def _drop_case_insensitive_indexes(db_engine):
    with db_engine.begin() as conn:
        conn.execute(text("DROP INDEX ux_project_project_id_lower"))
        conn.execute(text("DROP INDEX ux_instrument_instrument_id_lower"))


def test_reports_project_slugs_that_differ_only_by_case(db_engine):
    _drop_case_insensitive_indexes(db_engine)
    with Session(db_engine) as session:
        session.add(Project(project_id="Proj-A", unique_id="0proja000000000000000000a1", organization="x", project_lead_orcid="0000-0001"))
        session.add(Project(project_id="proj-a", unique_id="0proja000000000000000000a2", organization="x", project_lead_orcid="0000-0001"))
        session.add(Project(project_id="proj-b", unique_id="0projb000000000000000000b1", organization="x", project_lead_orcid="0000-0001"))
        session.commit()

        assert AUDIT_MODULE.find_case_collisions(session, "project") == [
            ("proj-a", [("Proj-A", "0proja000000000000000000a1"), ("proj-a", "0proja000000000000000000a2")]),
        ]


def test_reports_instrument_slugs_and_ignores_null_slugs(db_engine):
    _drop_case_insensitive_indexes(db_engine)
    with Session(db_engine) as session:
        for unique_id, slug in (("0i1", "XRD-1"), ("0i2", "xrd-1"), ("0i3", None), ("0i4", None)):
            session.add(Instrument(unique_id=unique_id, instrument_id=slug, instrument_name="n", owner="o", location="l"))
        session.commit()

        assert AUDIT_MODULE.find_case_collisions(session, "instrument") == [
            ("xrd-1", [("XRD-1", "0i1"), ("xrd-1", "0i2")]),
        ]


def test_reports_nothing_when_slugs_are_distinct(db_engine):
    with Session(db_engine) as session:
        session.add(Project(project_id="proj-a", unique_id="0proja000000000000000000a1", organization="x", project_lead_orcid="0000-0001"))
        session.commit()
        assert AUDIT_MODULE.find_case_collisions(session, "project") == []

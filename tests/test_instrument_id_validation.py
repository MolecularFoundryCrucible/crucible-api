import pytest
from fastapi import HTTPException
from sqlmodel import Session, SQLModel, create_engine
from sqlalchemy.pool import StaticPool

from app.models.core_models import Instrument, Project
from app.utils.instrument_utils import (
    assert_instrument_id_available, resolve_instrument_id_to_mfid,
    resolve_instrument_mfids_to_ids, validate_instrument_id_format,
)


class TestGrammar:
    @pytest.mark.parametrize("value", ["abc", "xrd-1", "XRD_1", "hip-microscope", "x" * 25])
    def test_well_formed_is_accepted(self, value):
        assert validate_instrument_id_format(value) == value

    @pytest.mark.parametrize("value,why", [
        ("", "empty"), ("a", "single character"), ("ab", "two characters"),
        ("has a space", "space"), ("-leading", "leading hyphen"),
        ("x" * 26, "too long"),
        ("xrd-1\n", "trailing newline"),
    ])
    def test_malformed_is_rejected(self, value, why):
        with pytest.raises(ValueError):
            validate_instrument_id_format(value)


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine, tables=[Instrument.__table__, Project.__table__])
    with Session(engine) as s:
        yield s


class TestCollision:
    def test_free_id_is_allowed(self, session):
        assert_instrument_id_available(session, "brand-new-instrument")

    def test_taken_id_is_rejected(self, session):
        session.add(Instrument(instrument_id="xrd-1", instrument_name="XRD-1", owner="o", location="b30"))
        session.commit()
        with pytest.raises(HTTPException) as exc:
            assert_instrument_id_available(session, "xrd-1")
        assert exc.value.status_code == 409

    def test_collision_is_case_insensitive(self, session):
        session.add(Instrument(instrument_id="xrd-1", instrument_name="XRD-1", owner="o", location="b30"))
        session.commit()
        with pytest.raises(HTTPException):
            assert_instrument_id_available(session, "XRD-1")

    def test_excluding_self_allows_unchanged_id(self, session):
        session.add(Instrument(unique_id="m1", instrument_id="xrd-1", instrument_name="XRD-1", owner="o", location="b30"))
        session.commit()
        assert_instrument_id_available(session, "xrd-1", exclude_unique_id="m1")

    def test_collides_with_an_existing_project_id(self, session):
        session.add(Project(project_id="proj-x", organization="LBNL", project_lead_orcid="0000-0001"))
        session.commit()
        with pytest.raises(HTTPException) as exc:
            assert_instrument_id_available(session, "proj-x")
        assert exc.value.status_code == 409

    def test_project_id_collision_is_case_insensitive(self, session):
        session.add(Project(project_id="Proj-X", organization="LBNL", project_lead_orcid="0000-0001"))
        session.commit()
        with pytest.raises(HTTPException):
            assert_instrument_id_available(session, "proj-x")


class TestResolution:
    def test_resolve_instrument_id_to_mfid(self, session):
        session.add(Instrument(unique_id="m1", instrument_id="xrd-1", instrument_name="XRD-1", owner="o", location="b30"))
        session.commit()
        assert resolve_instrument_id_to_mfid("xrd-1", session) == "m1"

    def test_resolve_unknown_instrument_id_returns_none(self, session):
        assert resolve_instrument_id_to_mfid("no-such-instrument", session) is None

    def test_resolve_instrument_mfids_to_ids_is_batched(self, session):
        session.add(Instrument(unique_id="m1", instrument_id="xrd-1", instrument_name="XRD-1", owner="o", location="b30"))
        session.add(Instrument(unique_id="m2", instrument_id="xrd-2", instrument_name="XRD-2", owner="o", location="b30"))
        session.commit()
        assert resolve_instrument_mfids_to_ids(["m1", "m2", "m3"], session) == {"m1": "xrd-1", "m2": "xrd-2"}

    def test_resolve_instrument_mfids_to_ids_empty_input(self, session):
        assert resolve_instrument_mfids_to_ids([], session) == {}
        assert resolve_instrument_mfids_to_ids([None, None], session) == {}

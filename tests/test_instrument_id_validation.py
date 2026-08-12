import pytest
from fastapi import HTTPException
from sqlmodel import Session, SQLModel, create_engine
from sqlalchemy.pool import StaticPool

from app.models.core_models import Instrument
from app.utils.instrument_utils import assert_instrument_id_available, validate_instrument_id_format


class TestGrammar:
    @pytest.mark.parametrize("value", ["xrd-1", "XRD_1", "hip-microscope", "a1"])
    def test_well_formed_is_accepted(self, value):
        assert validate_instrument_id_format(value) == value

    @pytest.mark.parametrize("value,why", [
        ("", "empty"), ("a", "single character"), ("has a space", "space"),
        ("-leading", "leading hyphen"), ("x" * 65, "too long"),
    ])
    def test_malformed_is_rejected(self, value, why):
        with pytest.raises(ValueError):
            validate_instrument_id_format(value)


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine, tables=[Instrument.__table__])
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

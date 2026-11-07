import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, SQLModel, select

from app.cli import auth as auth_cli
from app.config import ConfigurationError, get_settings
from app.models.core_models import LoginThrottle, AuthEvent, HubAccessToken, HubLoginCode, HubLoginTransaction, HubRefreshToken, HubTokenFamily
from app.utils.hub_auth_maintenance import cleanup_hub_auth

NOW = datetime.now(timezone.utc)


@pytest.fixture
def history(db_engine):
    SQLModel.metadata.create_all(db_engine, tables=[model.__table__ for model in (HubLoginTransaction, HubLoginCode, HubTokenFamily, HubRefreshToken, HubAccessToken, LoginThrottle)])
    with Session(db_engine) as session:
        for days, event in ((400, "sign_in"), (366, "provider_refused"), (364, "sign_in"), (10, "sign_in_failed"), (0, "identity_linked")):
            session.add(AuthEvent(at=NOW - timedelta(days=days), event=event))
        session.commit()
    return db_engine


def kept(engine):
    with Session(engine) as session:
        return sorted(event for event in session.exec(select(AuthEvent.event)).all())


def test_cleanup_removes_history_older_than_a_year_and_keeps_the_rest(history):
    assert cleanup_hub_auth(history, now=NOW)["auth_event"] == 2
    assert len(kept(history)) == 5
    assert cleanup_hub_auth(history, commit=True, now=NOW)["auth_event"] == 2
    assert kept(history) == ["identity_linked", "sign_in", "sign_in_failed"]


def test_retention_period_is_a_setting(history, monkeypatch):
    monkeypatch.setenv("CRUCIBLE_AUTH_EVENT_RETENTION_DAYS", "30")
    get_settings.cache_clear()
    assert cleanup_hub_auth(history, commit=True, now=NOW)["auth_event"] == 3
    assert kept(history) == ["identity_linked", "sign_in_failed"]


@pytest.mark.parametrize("value", ["0", "-5"])
def test_zero_or_negative_retention_is_refused(monkeypatch, value):
    monkeypatch.setenv("CRUCIBLE_AUTH_EVENT_RETENTION_DAYS", value)
    get_settings.cache_clear()
    with pytest.raises(ConfigurationError, match="CRUCIBLE_AUTH_EVENT_RETENTION_DAYS"):
        get_settings()


def test_cli_dry_run_reports_and_changes_nothing(history, use_engine, capsys):
    use_engine(history)
    assert auth_cli._run_cleanup(type("Args", (), {"dry_run": True, "batch": 1000, "batches": 100})()) == 0
    report = json.loads(capsys.readouterr().out.splitlines()[0])
    assert report["mode"] == "dry-run" and report["counts"]["auth_event"] >= 2
    assert len(kept(history)) == 5


def test_cli_cleans_in_batches_until_nothing_is_left(history, use_engine, capsys):
    use_engine(history)
    assert auth_cli._run_cleanup(type("Args", (), {"dry_run": False, "batch": 1, "batches": 100})()) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [line["counts"]["auth_event"] for line in lines] == [1, 1, 0]
    assert kept(history) == ["identity_linked", "sign_in", "sign_in_failed"]


def test_cli_refuses_bad_batch_sizes(capsys):
    assert auth_cli._run_cleanup(type("Args", (), {"dry_run": True, "batch": 0, "batches": 1})()) == 1

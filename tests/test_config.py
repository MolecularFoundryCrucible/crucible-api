import pytest

from app import config
from app.config import ConfigurationError, check_settings, env_flag, missing_required_settings, optional_env

BASE = ("DB_SERVER", "DB_USERNAME", "DB_DATABASE", "PGDB_PW", "CRUCIBLE_SECRET_KEY")
FEATURE_SETTINGS = (
    "INGESTION_ENABLED", "PRINTING_ENABLED", "PROPOSAL_SYNC_ENABLED",
    "RMQ_HOST", "RMQ_USER", "RMQ_ROUTING_SUFFIX", "RABBITMQ_DEFAULT_PW",
    "MQTT_HOST", "MQTT_USERNAME", "MQTT_PASSWORD", "PROPOSAL_DB_URL", "PROPDB_APIKEY",
)


@pytest.fixture
def empty(monkeypatch, tmp_path):
    monkeypatch.delenv("DB_CONF_FILE", raising=False)
    monkeypatch.setattr(config, "_loaded_env_files", set())
    monkeypatch.chdir(tmp_path)
    for name in BASE + FEATURE_SETTINGS:
        for suffix in ("", "_FILE", "_VERSION"):
            monkeypatch.delenv(f"{name}{suffix}", raising=False)
    return monkeypatch


def test_blank_values_are_unset(monkeypatch):
    monkeypatch.setenv("RECORD_BUCKET", "  ")
    assert optional_env("RECORD_BUCKET") is None
    monkeypatch.setenv("RECORD_BUCKET", " bucket ")
    assert optional_env("RECORD_BUCKET") == "bucket"


@pytest.mark.parametrize("value", ["TRUE", "1", "on", "yes"])
def test_env_flag_true(monkeypatch, value):
    monkeypatch.setenv("PRINTING_ENABLED", value)
    assert env_flag("PRINTING_ENABLED") is True


@pytest.mark.parametrize("value", ["false", "0", "Off", "no"])
def test_env_flag_false(monkeypatch, value):
    monkeypatch.setenv("PRINTING_ENABLED", value)
    assert env_flag("PRINTING_ENABLED", default=True) is False


def test_env_flag_default_and_typo(monkeypatch):
    monkeypatch.delenv("PRINTING_ENABLED", raising=False)
    assert env_flag("PRINTING_ENABLED") is False
    assert env_flag("PRINTING_ENABLED", default=True) is True
    monkeypatch.setenv("PRINTING_ENABLED", "ture")
    with pytest.raises(ConfigurationError, match="PRINTING_ENABLED"):
        env_flag("PRINTING_ENABLED")


def test_missing_required_lists_all_names(empty):
    empty.setenv("DB_SERVER", "db")
    missing = missing_required_settings()
    assert missing == ["CRUCIBLE_SECRET_KEY", "DB_DATABASE", "DB_USERNAME", "PGDB_PW"]
    empty.setenv("INGESTION_ENABLED", "true")
    missing = missing_required_settings()
    assert {"RMQ_HOST", "RMQ_USER", "RMQ_ROUTING_SUFFIX", "RABBITMQ_DEFAULT_PW"} <= set(missing)


def test_secret_satisfied_by_file_or_version(empty):
    for name in ("DB_SERVER", "DB_USERNAME", "DB_DATABASE"):
        empty.setenv(name, "x")
    empty.setenv("PGDB_PW_VERSION", "projects/1/secrets/p/versions/1")
    empty.setenv("CRUCIBLE_SECRET_KEY_FILE", "/run/secrets/key")
    assert missing_required_settings() == []


def test_check_settings_reports_everything_at_once(empty):
    with pytest.raises(ConfigurationError) as error:
        check_settings()
    for name in BASE:
        assert name in str(error.value)


def test_feature_requirements(empty):
    for name in BASE:
        empty.setenv(name, "x")
    empty.setenv("PRINTING_ENABLED", "true")
    empty.setenv("PROPOSAL_SYNC_ENABLED", "true")
    assert missing_required_settings() == ["MQTT_HOST", "MQTT_PASSWORD", "MQTT_USERNAME", "PROPDB_APIKEY", "PROPOSAL_DB_URL"]

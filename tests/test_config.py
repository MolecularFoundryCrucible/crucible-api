import pytest
from pydantic import SecretStr

from app import config
from app.config import ConfigurationError, check_settings, env_flag, get_settings, missing_required_settings, optional_env

BASE = ("DB_SERVER", "DB_USERNAME", "DB_DATABASE", "PGDB_PW", "CRUCIBLE_SECRET_KEY")
FEATURE_SETTINGS = (
    "INGESTION_ENABLED", "PRINTING_ENABLED", "PROPOSAL_SYNC_ENABLED",
    "RMQ_HOST", "RMQ_USER", "RMQ_ROUTING_SUFFIX", "RABBITMQ_DEFAULT_PW",
    "MQTT_HOST", "MQTT_USERNAME", "MQTT_PASSWORD", "PROPOSAL_DB_URL", "PROPDB_APIKEY",
)


@pytest.fixture
def empty(monkeypatch, tmp_path):
    monkeypatch.delenv("DB_CONF_FILE", raising=False)
    monkeypatch.delenv("CRUCIBLE_SECRETS_DIR", raising=False)
    monkeypatch.setattr(config, "_loaded_env_files", set())
    monkeypatch.setattr(config, "DEFAULT_SECRETS_DIR", str(tmp_path / "no-default-secrets"))
    monkeypatch.chdir(tmp_path)
    for name in BASE + FEATURE_SETTINGS:
        for suffix in ("", "_VERSION"):
            monkeypatch.delenv(f"{name}{suffix}", raising=False)
    return monkeypatch


def test_blank_values_are_unset(monkeypatch):
    monkeypatch.setenv("RECORD_BUCKET", "  ")
    assert optional_env("RECORD_BUCKET") is None
    monkeypatch.setenv("RECORD_BUCKET", " bucket ")
    assert optional_env("RECORD_BUCKET") == "bucket"


def test_settings_strip_plain_values_but_not_secrets(empty):
    empty.setenv("DB_SERVER", " db ")
    empty.setenv("RMQ_HOST", "   ")
    empty.setenv("PGDB_PW", " pw ")
    settings = get_settings()
    assert settings.db_server == "db"
    assert settings.rmq_host is None
    assert settings.pgdb_pw.get_secret_value() == " pw "


def test_secrets_are_hidden_when_printed(empty):
    empty.setenv("PGDB_PW", "hunter2")
    settings = get_settings()
    assert isinstance(settings.pgdb_pw, SecretStr)
    assert "hunter2" not in repr(settings) and "hunter2" not in str(settings.model_dump())


@pytest.mark.parametrize("value", ["TRUE", "1", "on", "yes"])
def test_env_flag_true(empty, value):
    empty.setenv("PRINTING_ENABLED", value)
    assert env_flag("PRINTING_ENABLED") is True


@pytest.mark.parametrize("value", ["false", "0", "Off", "no", " "])
def test_env_flag_false(empty, value):
    empty.setenv("PRINTING_ENABLED", value)
    assert env_flag("PRINTING_ENABLED") is False


def test_env_flag_default_and_typo(empty):
    assert env_flag("PRINTING_ENABLED") is False
    get_settings.cache_clear()
    empty.setenv("PRINTING_ENABLED", "ture")
    with pytest.raises(ConfigurationError, match="PRINTING_ENABLED"):
        env_flag("PRINTING_ENABLED")


def test_unknown_setting_name_rejected(empty):
    with pytest.raises(KeyError):
        env_flag("NOT_A_FLAG")


def test_missing_required_lists_all_names(empty):
    empty.setenv("DB_SERVER", "db")
    assert missing_required_settings() == ["CRUCIBLE_SECRET_KEY", "DB_DATABASE", "DB_USERNAME", "PGDB_PW"]
    get_settings.cache_clear()
    empty.setenv("INGESTION_ENABLED", "true")
    assert {"RMQ_HOST", "RMQ_USER", "RMQ_ROUTING_SUFFIX", "RABBITMQ_DEFAULT_PW"} <= set(missing_required_settings())


def test_secret_satisfied_by_secrets_directory_or_version(empty, tmp_path):
    for name in ("DB_SERVER", "DB_USERNAME", "DB_DATABASE"):
        empty.setenv(name, "x")
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "CRUCIBLE_SECRET_KEY").write_text("key")
    empty.setenv("CRUCIBLE_SECRETS_DIR", str(secrets))
    empty.setenv("PGDB_PW_VERSION", "projects/1/secrets/p/versions/1")
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


def test_settings_are_read_once(empty):
    empty.setenv("DB_SERVER", "first")
    assert get_settings().db_server == "first"
    empty.setenv("DB_SERVER", "second")
    assert get_settings().db_server == "first"


def test_env_flag_only_for_flags(empty):
    with pytest.raises(KeyError):
        env_flag("DB_SERVER")

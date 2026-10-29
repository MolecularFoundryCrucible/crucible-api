import pytest
from pydantic import SecretStr

from app import config
from app.config import ConfigurationError, check_settings, get_settings, missing_required_settings, optional_env

BASE = ("DB_SERVER", "DB_USERNAME", "DB_DATABASE", "PGDB_PW", "CRUCIBLE_SECRET_KEY")
FEATURE_SETTINGS = (
    "INGESTION_ENABLED", "PRINTING_ENABLED", "PROPOSAL_SYNC_ENABLED",
    "RMQ_HOST", "RMQ_USER", "RMQ_ROUTING_SUFFIX", "RABBITMQ_DEFAULT_PW",
    "MQTT_HOST", "MQTT_USERNAME", "MQTT_PASSWORD", "PROPOSAL_DB_URL", "PROPDB_APIKEY",
    "UPLOAD_BUCKET", "GCS_PROJECT", "GCS_SA", "STORAGE_READ_BUCKETS", "MULTIPART_UPLOAD_ENABLED", "RECORD_BUCKET",
    "STORAGE_BACKEND", "S3_ENDPOINT_URL", "S3_PUBLIC_URL", "S3_REGION", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY",
    "MULTIPART_UPLOAD_BUCKET", "MULTIPART_UPLOAD_SIGNER", "MULTIPART_UPLOAD_PROJECT", "FILE_VERSION_RETENTION_DAYS",
)


@pytest.fixture
def empty(monkeypatch, tmp_path):
    monkeypatch.delenv("DB_CONF_FILE", raising=False)
    monkeypatch.delenv("CRUCIBLE_SECRETS_DIR", raising=False)
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


def test_settings_trim_values_and_ignore_empty_ones(empty):
    empty.setenv("DB_SERVER", " db ")
    empty.setenv("RMQ_HOST", "")
    empty.setenv("PGDB_PW", " pw ")
    settings = get_settings()
    assert settings.db.server == "db"
    assert settings.ingestion.host is None
    assert settings.db.password.get_secret_value() == "pw"


def test_secrets_are_hidden_when_printed(empty):
    empty.setenv("PGDB_PW", "hunter2")
    settings = get_settings()
    assert isinstance(settings.db.password, SecretStr)
    assert "hunter2" not in repr(settings) and "hunter2" not in str(settings.model_dump())


@pytest.mark.parametrize("value", ["TRUE", "1", "on", "yes"])
def test_flag_true(empty, value):
    empty.setenv("PRINTING_ENABLED", value)
    assert get_settings().printing.enabled is True


@pytest.mark.parametrize("value", ["false", "0", "Off", "no", ""])
def test_flag_false(empty, value):
    empty.setenv("PRINTING_ENABLED", value)
    assert get_settings().printing.enabled is False


def test_flag_typo_is_a_configuration_error(empty):
    empty.setenv("PRINTING_ENABLED", "ture")
    with pytest.raises(ConfigurationError, match="PRINTING_ENABLED"):
        get_settings()


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


def test_version_only_counts_for_secrets(empty):
    for name in ("DB_USERNAME", "DB_DATABASE", "PGDB_PW", "CRUCIBLE_SECRET_KEY"):
        empty.setenv(name, "x")
    empty.setenv("DB_SERVER_VERSION", "projects/1/secrets/p/versions/1")
    assert missing_required_settings() == ["DB_SERVER"]


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
    assert get_settings().db.server == "first"
    empty.setenv("DB_SERVER", "second")
    assert get_settings().db.server == "first"


def test_whitespace_only_flag_counts_as_not_given(empty):
    empty.setenv("PRINTING_ENABLED", "   ")
    assert get_settings().printing.enabled is False


def test_whitespace_only_setting_counts_as_missing(empty):
    for name in ("DB_USERNAME", "DB_DATABASE", "PGDB_PW", "CRUCIBLE_SECRET_KEY"):
        empty.setenv(name, "x")
    empty.setenv("DB_SERVER", "   ")
    assert get_settings().db.server is None
    assert missing_required_settings() == ["DB_SERVER"]


def test_blank_secrets_file_keeps_the_default(empty, tmp_path):
    (tmp_path / "HUB_AUTH_CLIENT_ID").write_text("  \n")
    empty.setenv("CRUCIBLE_SECRETS_DIR", str(tmp_path))
    assert get_settings().hub.client_id == "crucible-hub"


def test_names_differing_only_in_case_are_refused(empty):
    empty.setenv("DB_SERVER", "one")
    empty.setenv("db_server", "two")
    with pytest.raises(ConfigurationError, match="differ only in case"):
        get_settings()


def test_lowercase_version_reference_is_found(empty, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "get_gcs_secret", lambda name: "from-manager")
    empty.setenv("pgdb_pw_version", "projects/1/secrets/pw/versions/2")
    assert config.get_secret("PGDB_PW") == "from-manager"


def test_hub_orcid_reference_must_be_valid_when_given(empty):
    for name in ("DB_SERVER", "DB_USERNAME", "DB_DATABASE", "PGDB_PW", "CRUCIBLE_SECRET_KEY", "HUB_AUTH_CLIENT_SECRET", "ORCID_CLIENT_SECRET"):
        empty.setenv(name, "x")
    empty.setenv("HUB_AUTH_ENABLED", "true")
    empty.setenv("HUB_AUTH_CALLBACK_URI", "https://example.test/hub/auth/callback")
    assert missing_required_settings() == []
    empty.setenv("HUB_AUTH_ORCID_CLIENT_SECRET_VERSION", "not-a-version")
    get_settings.cache_clear()
    assert missing_required_settings() == ["HUB_AUTH_ORCID_CLIENT_SECRET"]


def test_malformed_version_counts_as_missing(empty):
    for name in ("DB_SERVER", "DB_USERNAME", "DB_DATABASE", "CRUCIBLE_SECRET_KEY"):
        empty.setenv(name, "x")
    empty.setenv("PGDB_PW_VERSION", "pgcloud_pw/versions/2")
    assert missing_required_settings() == ["PGDB_PW"]


def _required(empty):
    for name in BASE:
        empty.setenv(name, "x")


def test_storage_is_off_when_not_configured(empty):
    _required(empty)
    assert missing_required_settings() == []


@pytest.mark.parametrize("setting", [("MULTIPART_UPLOAD_ENABLED", "true"), ("STORAGE_READ_BUCKETS", "old-bucket")])
def test_storage_requested_without_bucket_is_missing(empty, setting):
    _required(empty)
    empty.setenv(*setting)
    assert missing_required_settings() == ["GCS_SA", "UPLOAD_BUCKET"]


def test_record_bucket_needs_credentials_but_no_upload_bucket(empty):
    _required(empty)
    empty.setenv("RECORD_BUCKET", "records")
    assert missing_required_settings() == ["GCS_SA"]


def test_storage_bucket_needs_credentials(empty):
    _required(empty)
    empty.setenv("UPLOAD_BUCKET", "bucket")
    assert missing_required_settings() == ["GCS_SA"]
    get_settings.cache_clear()
    empty.setenv("GCS_SA_VERSION", "projects/1/secrets/sa/versions/latest")
    assert missing_required_settings() == []


def test_s3_storage_needs_its_endpoint_and_keys_but_not_gcs_credentials(empty):
    _required(empty)
    empty.setenv("STORAGE_BACKEND", "s3")
    empty.setenv("UPLOAD_BUCKET", "crucible-files")
    assert missing_required_settings() == ["S3_ACCESS_KEY_ID", "S3_ENDPOINT_URL", "S3_SECRET_ACCESS_KEY"]
    get_settings.cache_clear()
    for name, value in (("S3_ENDPOINT_URL", "http://seaweedfs:8333"), ("S3_ACCESS_KEY_ID", "key"), ("S3_SECRET_ACCESS_KEY", "secret")):
        empty.setenv(name, value)
    assert missing_required_settings() == []
    storage = get_settings().storage
    assert (storage.backend, storage.s3_region, storage.s3_public_url) == ("s3", "us-east-1", None)


def test_s3_record_bucket_alone_needs_the_s3_credentials(empty):
    _required(empty)
    empty.setenv("STORAGE_BACKEND", "s3")
    empty.setenv("RECORD_BUCKET", "crucible-records")
    assert missing_required_settings() == ["S3_ACCESS_KEY_ID", "S3_ENDPOINT_URL", "S3_SECRET_ACCESS_KEY"]


def test_unknown_storage_backend_is_refused(empty):
    _required(empty)
    empty.setenv("STORAGE_BACKEND", "azure")
    with pytest.raises(ConfigurationError, match="STORAGE_BACKEND"):
        get_settings()


def test_s3_multipart_needs_no_google_signer(empty):
    _required(empty)
    for name, value in (("STORAGE_BACKEND", "s3"), ("UPLOAD_BUCKET", "crucible-files"), ("MULTIPART_UPLOAD_ENABLED", "true"), ("MULTIPART_UPLOAD_BUCKET", "crucible-files"),
                        ("S3_ENDPOINT_URL", "http://seaweedfs:8333"), ("S3_ACCESS_KEY_ID", "key"), ("S3_SECRET_ACCESS_KEY", "secret")):
        empty.setenv(name, value)
    check_settings()
    get_settings.cache_clear()
    empty.setenv("STORAGE_BACKEND", "gcs")
    empty.setenv("GCS_SA", "{}")
    with pytest.raises(ConfigurationError, match="signer and a project"):
        check_settings()


def test_server_refuses_to_start_when_settings_are_missing(empty):
    from fastapi.testclient import TestClient

    from app.api.main import app

    _required(empty)
    empty.setenv("PRINTING_ENABLED", "true")
    with pytest.raises(ConfigurationError, match="MQTT_HOST"):
        with TestClient(app):
            pass


def test_invalid_multipart_settings_stop_the_check(empty):
    _required(empty)
    empty.setenv("MULTIPART_UPLOAD_ENABLED", "maybe")
    with pytest.raises(ConfigurationError, match="MULTIPART_UPLOAD_ENABLED must be true or false"):
        check_settings()


def test_send_pika_message_uses_configured_broker(empty, monkeypatch):
    from app.utils import rmq_utils

    empty.setenv("RMQ_HOST", "broker.lab")
    empty.setenv("RMQ_PORT", "5673")
    empty.setenv("RMQ_USER", "crucible")
    seen = {}

    class Connection:
        def __init__(self, parameters):
            seen["host"], seen["port"] = parameters.host, parameters.port
            seen["user"] = parameters.credentials.username

        def channel(self):
            return MagicMockChannel()

        def close(self):
            pass

    class MagicMockChannel:
        def basic_publish(self, **kwargs):
            seen["routing_key"] = kwargs["routing_key"]

    monkeypatch.setattr(rmq_utils.pika, "BlockingConnection", Connection)
    rmq_utils.send_pika_message("{}", "", "ingestion-lab-small", "pw")
    assert seen == {"host": "broker.lab", "port": 5673, "user": "crucible", "routing_key": "ingestion-lab-small"}


KEY_VERSION = "projects/1/secrets/crucible-key/versions/1"
HUB_VERSION = "projects/1/secrets/hub-client/versions/1"


@pytest.fixture
def startup(empty):
    for name in ("HUB_AUTH_ENABLED", "HUB_AUTH_CALLBACK_URI", "HUB_AUTH_CLIENT_SECRET", "ORCID_CLIENT_SECRET",
                 "HUB_AUTH_ORCID_CLIENT_SECRET", "CRUCIBLE_CURSOR_SIGNING_KEY", "CRUCIBLE_CURSOR_SIGNING_KEYS"):
        for suffix in ("", "_VERSION", "_FILE"):
            empty.delenv(f"{name}{suffix}", raising=False)
    for name in BASE:
        empty.setenv(name, "x")
    empty.delenv("CRUCIBLE_SECRET_KEY")
    empty.setenv("CRUCIBLE_SECRET_KEY_VERSION", KEY_VERSION)
    fetched = []

    def secret_manager(version):
        fetched.append(version)
        return f"value of {version}"

    empty.setattr(config, "get_gcs_secret", secret_manager)
    return fetched


def test_read_secrets_fills_the_cache_requests_read_from(startup):
    config.read_secrets()
    assert startup == [KEY_VERSION]
    assert config.get_secret("CRUCIBLE_SECRET_KEY") == f"value of {KEY_VERSION}"
    assert startup == [KEY_VERSION]


def test_read_secrets_skips_features_that_are_off(startup, empty):
    empty.setenv("RABBITMQ_DEFAULT_PW_VERSION", "projects/1/secrets/rmq/versions/1")
    config.read_secrets()
    assert startup == [KEY_VERSION]
    for name, value in (("INGESTION_ENABLED", "true"), ("RMQ_HOST", "h"), ("RMQ_USER", "u"), ("RMQ_ROUTING_SUFFIX", "s")):
        empty.setenv(name, value)
    get_settings.cache_clear()
    config.read_secrets()
    assert "projects/1/secrets/rmq/versions/1" in startup


def test_read_secrets_skips_storage_when_it_is_not_configured(startup, empty):
    empty.setenv("GCS_SA_VERSION", "projects/1/secrets/sa/versions/1")
    config.read_secrets()
    assert startup == [KEY_VERSION]
    empty.setenv("RECORD_BUCKET", "records")
    get_settings.cache_clear()
    config.read_secrets()
    assert "projects/1/secrets/sa/versions/1" in startup


def test_read_secrets_names_the_secret_it_cannot_read(startup, empty):
    from google.api_core.exceptions import PermissionDenied

    def denied(version):
        raise PermissionDenied("no access")

    empty.setattr(config, "get_gcs_secret", denied)
    with pytest.raises(ConfigurationError, match="CRUCIBLE_SECRET_KEY_VERSION could not be read: PermissionDenied"):
        config.read_secrets()


def test_read_secrets_does_not_echo_undecodable_secret_bytes(startup, empty):
    def undecodable(version):
        return b"\xffsecret".decode("utf-8")

    empty.setattr(config, "get_gcs_secret", undecodable)
    with pytest.raises(ConfigurationError) as error:
        config.read_secrets()
    assert str(error.value) == "CRUCIBLE_SECRET_KEY_VERSION could not be read: UnicodeDecodeError"
    assert error.value.__cause__ is None and error.value.__suppress_context__


def test_read_secrets_refuses_an_empty_secret(startup, empty):
    empty.setattr(config, "get_gcs_secret", lambda version: "\n")
    with pytest.raises(ConfigurationError, match="CRUCIBLE_SECRET_KEY_VERSION names an empty secret"):
        config.read_secrets()


def test_read_secrets_checks_hub_secrets_with_the_same_reader(startup, empty):
    for name, value in (("HUB_AUTH_ENABLED", "true"), ("HUB_AUTH_CALLBACK_URI", "https://lab/cb"),
                        ("HUB_AUTH_CLIENT_SECRET_VERSION", HUB_VERSION), ("ORCID_CLIENT_SECRET", "orcid")):
        empty.setenv(name, value)
    config.read_secrets()
    assert sorted(startup) == sorted([HUB_VERSION, KEY_VERSION])
    empty.setattr(config, "get_gcs_secret", lambda version: "two\nlines")
    config._fetched_secrets.pop(HUB_VERSION)
    with pytest.raises(ConfigurationError, match="HUB_AUTH_CLIENT_SECRET must be one line of printable ASCII"):
        config.read_secrets()


def test_a_worker_that_cannot_read_a_secret_does_not_start(startup, empty):
    from fastapi.testclient import TestClient

    from app.api import main

    def denied(version):
        raise RuntimeError("secret manager down")

    empty.setattr(config, "get_gcs_secret", denied)
    with pytest.raises(ConfigurationError, match="CRUCIBLE_SECRET_KEY_VERSION could not be read: RuntimeError"):
        with TestClient(main.app):
            pass


def test_a_worker_starts_with_its_secrets_already_read(startup):
    from fastapi.testclient import TestClient

    from app.api import main

    with TestClient(main.app):
        assert startup == [KEY_VERSION]
        assert config.get_secret("CRUCIBLE_SECRET_KEY") == f"value of {KEY_VERSION}"
        assert startup == [KEY_VERSION]


def test_s3_keys_must_be_one_line(empty):
    empty.setenv("S3_SECRET_ACCESS_KEY", "two\nlines")
    with pytest.raises(ConfigurationError, match="S3_SECRET_ACCESS_KEY must be one line of printable ASCII"):
        config.get_secret("S3_SECRET_ACCESS_KEY")

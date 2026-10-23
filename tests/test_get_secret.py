import os

import pytest

from app import config, general_utils
from app.config import ConfigurationError

VERSION = "projects/123/secrets/db-password/versions/2"
SECRET = "PROPDB_APIKEY"


@pytest.fixture
def no_secret_manager(monkeypatch):
    def refuse(name):
        raise AssertionError(f"Secret Manager called for {name}")

    monkeypatch.setattr(general_utils, "get_gcs_secret", refuse)


@pytest.fixture
def clean(monkeypatch, tmp_path):
    for name in (SECRET, "CRUCIBLE_TEST_SECRET"):
        for suffix in ("", "_VERSION"):
            monkeypatch.delenv(f"{name}{suffix}", raising=False)
    monkeypatch.delenv("DB_CONF_FILE", raising=False)
    monkeypatch.delenv("CRUCIBLE_SECRETS_DIR", raising=False)
    monkeypatch.setattr(config, "_loaded_env_files", set())
    monkeypatch.setattr(config, "DEFAULT_SECRETS_DIR", str(tmp_path / "no-default-secrets"))
    monkeypatch.chdir(tmp_path)
    yield tmp_path
    for name in (SECRET, "CRUCIBLE_TEST_SECRET"):
        for suffix in ("", "_VERSION"):
            os.environ.pop(f"{name}{suffix}", None)


def _secrets_dir(clean, monkeypatch, files):
    directory = clean / "secrets"
    directory.mkdir()
    for name, content in files.items():
        (directory / name).write_bytes(content.encode())
    monkeypatch.setenv("CRUCIBLE_SECRETS_DIR", str(directory))
    return directory


def test_missing_secret_names_every_way_to_set_it(clean):
    with pytest.raises(ConfigurationError, match=f"{SECRET} is not set .*secrets directory.*_VERSION"):
        general_utils.get_secret(SECRET)


def test_configuration_error_is_a_runtime_error():
    assert issubclass(ConfigurationError, RuntimeError)


def test_environment_value_wins(clean, monkeypatch, no_secret_manager):
    _secrets_dir(clean, monkeypatch, {SECRET: "from-file"})
    monkeypatch.setenv(SECRET, "from-env")
    monkeypatch.setenv(f"{SECRET}_VERSION", VERSION)
    assert general_utils.get_secret(SECRET) == "from-env"


def test_secrets_directory_wins_over_version(clean, monkeypatch, no_secret_manager):
    _secrets_dir(clean, monkeypatch, {SECRET: "from-file\n"})
    monkeypatch.setenv(SECRET, " ")
    monkeypatch.setenv(f"{SECRET}_VERSION", VERSION)
    assert general_utils.get_secret(SECRET) == "from-file"


def test_secrets_directory_file_names_are_case_insensitive(clean, monkeypatch, no_secret_manager):
    _secrets_dir(clean, monkeypatch, {SECRET.lower(): "lower"})
    assert general_utils.get_secret(SECRET) == "lower"


def test_default_secrets_directory_used_when_present(clean, monkeypatch, no_secret_manager):
    directory = clean / "run-secrets"
    directory.mkdir()
    (directory / SECRET).write_text("mounted")
    monkeypatch.setattr(config, "DEFAULT_SECRETS_DIR", str(directory))
    assert general_utils.get_secret(SECRET) == "mounted"


def test_configured_secrets_directory_must_exist(clean, monkeypatch):
    monkeypatch.setenv("CRUCIBLE_SECRETS_DIR", str(clean / "absent"))
    with pytest.raises(ConfigurationError, match="CRUCIBLE_SECRETS_DIR"):
        general_utils.get_secret(SECRET)


def test_blank_environment_value_is_unset(clean, monkeypatch):
    monkeypatch.setenv(SECRET, "   ")
    with pytest.raises(ConfigurationError):
        general_utils.get_secret(SECRET)


def test_secret_value_keeps_inner_whitespace(clean, monkeypatch):
    monkeypatch.setenv(SECRET, " spaced secret ")
    assert general_utils.get_secret(SECRET) == " spaced secret "


@pytest.mark.parametrize("version", [VERSION, "projects/776258882599/secrets/service-account-cred/versions/latest"])
def test_version_reads_secret_manager_once_and_keeps_it_in_memory(clean, monkeypatch, version):
    calls = []

    def fake(name):
        calls.append(name)
        return "from-manager"

    monkeypatch.setattr(general_utils, "get_gcs_secret", fake)
    monkeypatch.setenv(f"{SECRET}_VERSION", version)
    assert general_utils.get_secret(SECRET) == "from-manager"
    assert general_utils.get_secret(SECRET) == "from-manager"
    assert calls == [version]
    assert SECRET not in os.environ


def test_changed_version_is_fetched_again(clean, monkeypatch):
    monkeypatch.setattr(general_utils, "get_gcs_secret", lambda name: name.rsplit("/", 1)[1])
    monkeypatch.setenv(f"{SECRET}_VERSION", "projects/1/secrets/s/versions/1")
    assert general_utils.get_secret(SECRET) == "1"
    monkeypatch.setenv(f"{SECRET}_VERSION", "projects/1/secrets/s/versions/2")
    assert general_utils.get_secret(SECRET) == "2"


def test_empty_secret_manager_value_rejected(clean, monkeypatch):
    monkeypatch.setattr(general_utils, "get_gcs_secret", lambda name: "  ")
    monkeypatch.setenv(f"{SECRET}_VERSION", VERSION)
    with pytest.raises(ConfigurationError, match="empty secret"):
        general_utils.get_secret(SECRET)


@pytest.mark.parametrize("version", [
    "pgcloud_pw/versions/2",
    "projects/123/secrets/x",
    "projects/1/secrets/x/versions/1/extra",
    "projects/1/secrets/x/versions/0",
    "projects/../secrets/x/versions/1",
])
def test_relative_or_malformed_version_rejected(clean, monkeypatch, no_secret_manager, version):
    monkeypatch.setenv(f"{SECRET}_VERSION", version)
    with pytest.raises(ConfigurationError, match=f"{SECRET}_VERSION"):
        general_utils.get_secret(SECRET)


def test_name_outside_settings_reads_environment_and_version(clean, monkeypatch):
    monkeypatch.setenv("CRUCIBLE_TEST_SECRET", "plain")
    assert general_utils.get_secret("CRUCIBLE_TEST_SECRET") == "plain"
    monkeypatch.delenv("CRUCIBLE_TEST_SECRET")
    monkeypatch.setattr(general_utils, "get_gcs_secret", lambda name: "fetched")
    monkeypatch.setenv("CRUCIBLE_TEST_SECRET_VERSION", VERSION)
    assert general_utils.get_secret("CRUCIBLE_TEST_SECRET") == "fetched"


def test_get_gcs_secret_uses_default_credentials(monkeypatch):
    from google.cloud import secretmanager

    seen = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            seen["args"] = (args, kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            seen["closed"] = True

        def access_secret_version(self, name):
            seen["name"] = name

            class Payload:
                data = "s3cret".encode()

            class Response:
                payload = Payload()

            return Response()

    monkeypatch.setattr(secretmanager, "SecretManagerServiceClient", FakeClient)
    assert general_utils.get_gcs_secret(VERSION) == "s3cret"
    assert seen == {"args": ((), {}), "name": VERSION, "closed": True}


def test_no_parent_dotenv_is_loaded(clean, monkeypatch):
    (clean / ".env").write_text(f"{SECRET}=leaked\n")
    child = clean / "checkout"
    child.mkdir()
    monkeypatch.chdir(child)
    with pytest.raises(ConfigurationError):
        general_utils.get_secret(SECRET)


def test_dotenv_in_working_directory_is_loaded(clean):
    (clean / ".env").write_text(f"{SECRET}=local\n")
    assert general_utils.get_secret(SECRET) == "local"


def test_explicit_env_file_must_exist(clean, monkeypatch):
    monkeypatch.setenv("DB_CONF_FILE", str(clean / "missing.env"))
    with pytest.raises(ConfigurationError, match="missing.env"):
        general_utils.get_secret(SECRET)


def test_explicit_file_is_not_mixed_with_working_directory_dotenv(clean, monkeypatch):
    explicit = clean / "staging.env"
    explicit.write_text(f"{SECRET}_VERSION={VERSION}\n")
    (clean / ".env").write_text(f"{SECRET}=local\n")
    monkeypatch.setattr(general_utils, "get_gcs_secret", lambda name: "from-manager")
    config.load_env_file(str(explicit))
    assert general_utils.get_secret(SECRET) == "from-manager"


def test_kubernetes_service_account_mount_is_harmless(clean, monkeypatch):
    directory = clean / "run-secrets"
    (directory / "kubernetes.io" / "serviceaccount").mkdir(parents=True)
    (directory / "kubernetes.io" / "serviceaccount" / "token").write_text("not-a-setting")
    monkeypatch.setattr(config, "DEFAULT_SECRETS_DIR", str(directory))
    monkeypatch.setattr(general_utils, "get_gcs_secret", lambda name: "from-manager")
    monkeypatch.setenv(f"{SECRET}_VERSION", VERSION)
    assert config.setting_value(SECRET) is None
    assert config.setting_value("PRINTING_ENABLED") is False
    assert general_utils.get_secret(SECRET) == "from-manager"


def test_name_outside_settings_reads_secrets_directory(clean, monkeypatch, no_secret_manager):
    _secrets_dir(clean, monkeypatch, {"CRUCIBLE_TEST_SECRET": "from-file\n"})
    assert general_utils.get_secret("CRUCIBLE_TEST_SECRET") == "from-file"


def test_only_secret_fields_are_read_as_secrets(clean):
    assert config.is_secret_setting("PROPDB_APIKEY")
    assert not config.is_secret_setting("PRINTING_ENABLED")
    assert not config.is_secret_setting("DB_SERVER")
    assert not config.is_secret_setting("CRUCIBLE_TEST_SECRET")


def test_unreadable_secret_file_is_a_configuration_error(clean, monkeypatch):
    _secrets_dir(clean, monkeypatch, {SECRET: ""})
    (clean / "secrets" / SECRET).write_bytes(b"\xff\xfe")
    with pytest.raises(ConfigurationError, match="Unreadable file in the secrets directory"):
        general_utils.get_secret(SECRET)


def test_loading_an_explicit_file_refreshes_settings(clean, monkeypatch):
    assert config.setting_value(SECRET) is None
    explicit = clean / "staging.env"
    explicit.write_text(f"{SECRET}=from-explicit\n")
    config.load_env_file(str(explicit))
    assert general_utils.get_secret(SECRET) == "from-explicit"

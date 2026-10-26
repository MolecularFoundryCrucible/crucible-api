import os

import pytest

from app import config
from app.config import ConfigurationError, get_secret

VERSION = "projects/123/secrets/db-password/versions/2"
SECRET = "PROPDB_APIKEY"


@pytest.fixture
def no_secret_manager(monkeypatch):
    def refuse(name):
        raise AssertionError(f"Secret Manager called for {name}")

    monkeypatch.setattr(config, "get_gcs_secret", refuse)


@pytest.fixture
def clean(monkeypatch, tmp_path):
    for suffix in ("", "_VERSION"):
        monkeypatch.delenv(f"{SECRET}{suffix}", raising=False)
    monkeypatch.delenv("DB_CONF_FILE", raising=False)
    monkeypatch.delenv("CRUCIBLE_SECRETS_DIR", raising=False)
    monkeypatch.setattr(config, "DEFAULT_SECRETS_DIR", str(tmp_path / "no-default-secrets"))
    monkeypatch.chdir(tmp_path)
    yield tmp_path
    for suffix in ("", "_VERSION"):
        os.environ.pop(f"{SECRET}{suffix}", None)


def _secrets_dir(clean, monkeypatch, files):
    directory = clean / "secrets"
    directory.mkdir()
    for name, content in files.items():
        (directory / name).write_bytes(content.encode() if isinstance(content, str) else content)
    monkeypatch.setenv("CRUCIBLE_SECRETS_DIR", str(directory))
    return directory


def test_missing_secret_names_every_way_to_set_it(clean):
    with pytest.raises(ConfigurationError, match=f"{SECRET} is not set .*secrets directory.*_VERSION"):
        get_secret(SECRET)


def test_configuration_error_is_a_runtime_error():
    assert issubclass(ConfigurationError, RuntimeError)


@pytest.mark.parametrize("name", ["DB_SERVER", "PRINTING_ENABLED", "NOT_A_SETTING"])
def test_only_secret_settings_can_be_read(clean, name):
    with pytest.raises(KeyError):
        get_secret(name)


@pytest.mark.parametrize("ways", [
    ("environment", "directory"),
    ("directory", "version"),
    ("environment", "version"),
    ("pointer", "environment"),
    ("pointer", "directory"),
])
def test_a_secret_given_two_ways_is_refused(clean, monkeypatch, no_secret_manager, ways):
    if "directory" in ways:
        _secrets_dir(clean, monkeypatch, {SECRET: "from-directory"})
    if "environment" in ways:
        monkeypatch.setenv(SECRET, "from-environment")
    if "version" in ways:
        monkeypatch.setenv(f"{SECRET}_VERSION", VERSION)
    if "pointer" in ways:
        pointed = clean / "pointed"
        pointed.write_text("from-pointer")
        monkeypatch.setenv(f"{SECRET}_FILE", str(pointed))
    with pytest.raises(ConfigurationError, match=f"{SECRET} is given more than one way") as error:
        get_secret(SECRET)
    assert "from-" not in str(error.value)


def test_secret_file_named_by_file_variable(clean, monkeypatch, no_secret_manager):
    pointed = clean / "elsewhere" / "apikey"
    pointed.parent.mkdir()
    pointed.write_text("from-pointer\n")
    monkeypatch.setenv(f"{SECRET}_FILE", str(pointed))
    assert get_secret(SECRET) == "from-pointer"


@pytest.mark.parametrize("content", [None, "", " \n"])
def test_missing_or_empty_pointed_file_is_refused(clean, monkeypatch, content):
    pointed = clean / "apikey"
    if content is not None:
        pointed.write_text(content)
    monkeypatch.setenv(f"{SECRET}_FILE", str(pointed))
    with pytest.raises(ConfigurationError, match=f"{SECRET}_FILE names"):
        get_secret(SECRET)


def test_secrets_directory_value_is_trimmed(clean, monkeypatch, no_secret_manager):
    _secrets_dir(clean, monkeypatch, {SECRET: "from-file\n"})
    assert get_secret(SECRET) == "from-file"


def test_secrets_directory_file_names_are_case_insensitive(clean, monkeypatch, no_secret_manager):
    _secrets_dir(clean, monkeypatch, {SECRET.lower(): "lower"})
    assert get_secret(SECRET) == "lower"


def test_default_secrets_directory_used_when_present(clean, monkeypatch, no_secret_manager):
    directory = clean / "run-secrets"
    directory.mkdir()
    (directory / SECRET).write_text("mounted")
    monkeypatch.setattr(config, "DEFAULT_SECRETS_DIR", str(directory))
    assert get_secret(SECRET) == "mounted"


def test_configured_secrets_directory_must_exist(clean, monkeypatch):
    monkeypatch.setenv("CRUCIBLE_SECRETS_DIR", str(clean / "absent"))
    with pytest.raises(ConfigurationError, match="CRUCIBLE_SECRETS_DIR"):
        get_secret(SECRET)


def test_unreadable_secret_file_is_a_configuration_error(clean, monkeypatch):
    _secrets_dir(clean, monkeypatch, {SECRET: b"\xff\xfe"})
    with pytest.raises(ConfigurationError, match="Unreadable file in the secrets directory"):
        get_secret(SECRET)


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_value_is_unset(clean, monkeypatch, value):
    monkeypatch.setenv(SECRET, value)
    with pytest.raises(ConfigurationError, match="is not set"):
        get_secret(SECRET)


def test_values_are_trimmed(clean, monkeypatch):
    monkeypatch.setenv(SECRET, " spaced secret \n")
    assert get_secret(SECRET) == "spaced secret"


@pytest.mark.parametrize("version", [VERSION, "projects/776258882599/secrets/service-account-cred/versions/latest"])
def test_version_reads_secret_manager_once_and_keeps_it_in_memory(clean, monkeypatch, version):
    calls = []

    def fake(name):
        calls.append(name)
        return "from-manager"

    monkeypatch.setattr(config, "get_gcs_secret", fake)
    monkeypatch.setenv(f"{SECRET}_VERSION", version)
    assert get_secret(SECRET) == "from-manager"
    assert get_secret(SECRET) == "from-manager"
    assert calls == [version]
    assert SECRET not in os.environ


def test_changed_version_is_fetched_again(clean, monkeypatch):
    monkeypatch.setattr(config, "get_gcs_secret", lambda name: name.rsplit("/", 1)[1])
    monkeypatch.setenv(f"{SECRET}_VERSION", "projects/1/secrets/s/versions/1")
    assert get_secret(SECRET) == "1"
    monkeypatch.setenv(f"{SECRET}_VERSION", "projects/1/secrets/s/versions/2")
    assert get_secret(SECRET) == "2"


def test_empty_secret_manager_value_rejected(clean, monkeypatch):
    monkeypatch.setattr(config, "get_gcs_secret", lambda name: "  ")
    monkeypatch.setenv(f"{SECRET}_VERSION", VERSION)
    with pytest.raises(ConfigurationError, match="empty secret"):
        get_secret(SECRET)


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
        get_secret(SECRET)


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
    assert config.get_gcs_secret(VERSION) == "s3cret"
    assert seen == {"args": ((), {}), "name": VERSION, "closed": True}


def test_kubernetes_service_account_mount_is_harmless(clean, monkeypatch):
    directory = clean / "run-secrets"
    (directory / "kubernetes.io" / "serviceaccount").mkdir(parents=True)
    (directory / "kubernetes.io" / "serviceaccount" / "token").write_text("not-a-setting")
    monkeypatch.setattr(config, "DEFAULT_SECRETS_DIR", str(directory))
    monkeypatch.setattr(config, "get_gcs_secret", lambda name: "from-manager")
    monkeypatch.setenv(f"{SECRET}_VERSION", VERSION)
    assert config.get_settings().proposals.api_key is None
    assert config.get_settings().printing.port == 8883
    assert get_secret(SECRET) == "from-manager"


def test_no_parent_dotenv_is_loaded(clean, monkeypatch):
    (clean / ".env").write_text(f"{SECRET}=leaked\n")
    child = clean / "checkout"
    child.mkdir()
    monkeypatch.chdir(child)
    with pytest.raises(ConfigurationError):
        get_secret(SECRET)


def test_dotenv_in_working_directory_is_loaded(clean):
    (clean / ".env").write_text(f"{SECRET}=local\n")
    assert get_secret(SECRET) == "local"


def test_explicit_env_file_must_exist(clean, monkeypatch):
    monkeypatch.setenv("DB_CONF_FILE", str(clean / "missing.env"))
    with pytest.raises(ConfigurationError, match="missing.env"):
        get_secret(SECRET)


def test_explicit_file_is_not_mixed_with_working_directory_dotenv(clean, monkeypatch):
    explicit = clean / "staging.env"
    explicit.write_text(f"{SECRET}_VERSION={VERSION}\n")
    (clean / ".env").write_text(f"{SECRET}=local\n")
    monkeypatch.setattr(config, "get_gcs_secret", lambda name: "from-manager")
    config.load_env_file(str(explicit))
    assert get_secret(SECRET) == "from-manager"


def test_loading_an_explicit_file_refreshes_settings(clean):
    assert config.get_settings().proposals.api_key is None
    explicit = clean / "staging.env"
    explicit.write_text(f"{SECRET}=from-explicit\n")
    config.load_env_file(str(explicit))
    assert get_secret(SECRET) == "from-explicit"


def test_whitespace_only_environment_value_counts_as_unset(clean, monkeypatch):
    monkeypatch.setenv(SECRET, "   ")
    with pytest.raises(ConfigurationError, match="is not set"):
        get_secret(SECRET)


def test_version_lookup_uses_the_upper_case_name(clean, monkeypatch):
    monkeypatch.setattr(config, "get_gcs_secret", lambda name: "from-manager")
    monkeypatch.setenv(f"{SECRET}_VERSION", VERSION)
    assert get_secret(SECRET.lower()) == "from-manager"


def test_secret_manager_value_is_returned_as_stored(clean, monkeypatch):
    monkeypatch.setattr(config, "get_gcs_secret", lambda name: "stored-with-newline\n")
    monkeypatch.setenv(f"{SECRET}_VERSION", VERSION)
    assert get_secret(SECRET) == "stored-with-newline\n"

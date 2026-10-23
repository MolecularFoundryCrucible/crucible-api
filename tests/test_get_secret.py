import os

import pytest

from app import config, general_utils
from app.config import ConfigurationError

VERSION = "projects/123/secrets/db-password/versions/2"


@pytest.fixture
def no_secret_manager(monkeypatch):
    def refuse(name):
        raise AssertionError(f"Secret Manager called for {name}")

    monkeypatch.setattr(general_utils, "get_gcs_secret", refuse)


@pytest.fixture
def clean(monkeypatch, tmp_path):
    for suffix in ("", "_FILE", "_VERSION"):
        monkeypatch.delenv(f"CRUCIBLE_TEST_SECRET{suffix}", raising=False)
    monkeypatch.delenv("DB_CONF_FILE", raising=False)
    monkeypatch.setattr(config, "_loaded_env_files", set())
    monkeypatch.chdir(tmp_path)
    yield tmp_path
    for suffix in ("", "_FILE", "_VERSION"):
        os.environ.pop(f"CRUCIBLE_TEST_SECRET{suffix}", None)


def test_missing_secret_names_every_way_to_set_it(clean):
    with pytest.raises(ConfigurationError, match="CRUCIBLE_TEST_SECRET is not set .*_FILE.*_VERSION"):
        general_utils.get_secret("CRUCIBLE_TEST_SECRET")


def test_configuration_error_is_a_runtime_error():
    assert issubclass(ConfigurationError, RuntimeError)


def test_environment_value_wins(clean, monkeypatch, no_secret_manager):
    secret_file = clean / "secret"
    secret_file.write_text("from-file")
    monkeypatch.setenv("CRUCIBLE_TEST_SECRET", "from-env")
    monkeypatch.setenv("CRUCIBLE_TEST_SECRET_FILE", str(secret_file))
    monkeypatch.setenv("CRUCIBLE_TEST_SECRET_VERSION", VERSION)
    assert general_utils.get_secret("CRUCIBLE_TEST_SECRET") == "from-env"


def test_file_wins_over_version(clean, monkeypatch, no_secret_manager):
    secret_file = clean / "secret"
    secret_file.write_text("from-file")
    monkeypatch.setenv("CRUCIBLE_TEST_SECRET", " ")
    monkeypatch.setenv("CRUCIBLE_TEST_SECRET_FILE", str(secret_file))
    monkeypatch.setenv("CRUCIBLE_TEST_SECRET_VERSION", VERSION)
    assert general_utils.get_secret("CRUCIBLE_TEST_SECRET") == "from-file"


def test_blank_environment_value_is_unset(clean, monkeypatch):
    monkeypatch.setenv("CRUCIBLE_TEST_SECRET", "   ")
    with pytest.raises(ConfigurationError):
        general_utils.get_secret("CRUCIBLE_TEST_SECRET")


@pytest.mark.parametrize("content, expected", [("s3cret\n", "s3cret"), ("s3cret\r\n", "s3cret"), ('{"a": 1,\n "b": 2}\n', '{"a": 1,\n "b": 2}')])
def test_file_is_read_without_trailing_newline(clean, monkeypatch, no_secret_manager, content, expected):
    secret_file = clean / "secret"
    secret_file.write_bytes(content.encode())
    monkeypatch.setenv("CRUCIBLE_TEST_SECRET_FILE", str(secret_file))
    assert general_utils.get_secret("CRUCIBLE_TEST_SECRET") == expected


@pytest.mark.parametrize("content", [b"", b"\n", b"   \n"])
def test_empty_file_rejected(clean, monkeypatch, no_secret_manager, content):
    secret_file = clean / "secret"
    secret_file.write_bytes(content)
    monkeypatch.setenv("CRUCIBLE_TEST_SECRET_FILE", str(secret_file))
    with pytest.raises(ConfigurationError, match="CRUCIBLE_TEST_SECRET_FILE points to an empty file"):
        general_utils.get_secret("CRUCIBLE_TEST_SECRET")


def test_unreadable_file_is_named(clean, monkeypatch):
    bad = clean / "bad"
    bad.write_bytes(b"\xff\xfe\xfa")
    monkeypatch.setenv("CRUCIBLE_TEST_SECRET_FILE", str(bad))
    with pytest.raises(ConfigurationError, match="CRUCIBLE_TEST_SECRET_FILE"):
        general_utils.get_secret("CRUCIBLE_TEST_SECRET")
    monkeypatch.setenv("CRUCIBLE_TEST_SECRET_FILE", str(clean / "absent"))
    with pytest.raises(ConfigurationError, match="CRUCIBLE_TEST_SECRET_FILE"):
        general_utils.get_secret("CRUCIBLE_TEST_SECRET")


@pytest.mark.parametrize("version", [VERSION, "projects/776258882599/secrets/service-account-cred/versions/latest"])
def test_version_reads_secret_manager_and_caches(clean, monkeypatch, version):
    calls = []

    def fake(name):
        calls.append(name)
        return "from-manager"

    monkeypatch.setattr(general_utils, "get_gcs_secret", fake)
    monkeypatch.setenv("CRUCIBLE_TEST_SECRET_VERSION", version)
    assert general_utils.get_secret("CRUCIBLE_TEST_SECRET") == "from-manager"
    assert general_utils.get_secret("CRUCIBLE_TEST_SECRET") == "from-manager"
    assert calls == [version]


def test_empty_secret_manager_value_rejected(clean, monkeypatch):
    monkeypatch.setattr(general_utils, "get_gcs_secret", lambda name: "  ")
    monkeypatch.setenv("CRUCIBLE_TEST_SECRET_VERSION", VERSION)
    with pytest.raises(ConfigurationError, match="empty secret"):
        general_utils.get_secret("CRUCIBLE_TEST_SECRET")


@pytest.mark.parametrize("version", [
    "pgcloud_pw/versions/2",
    "projects/123/secrets/x",
    "projects/1/secrets/x/versions/1/extra",
    "projects/1/secrets/x/versions/0",
    "projects/../secrets/x/versions/1",
])
def test_relative_or_malformed_version_rejected(clean, monkeypatch, no_secret_manager, version):
    monkeypatch.setenv("CRUCIBLE_TEST_SECRET_VERSION", version)
    with pytest.raises(ConfigurationError, match="CRUCIBLE_TEST_SECRET_VERSION"):
        general_utils.get_secret("CRUCIBLE_TEST_SECRET")


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
    (clean / ".env").write_text("CRUCIBLE_TEST_SECRET=leaked\n")
    child = clean / "checkout"
    child.mkdir()
    monkeypatch.chdir(child)
    with pytest.raises(ConfigurationError):
        general_utils.get_secret("CRUCIBLE_TEST_SECRET")


def test_dotenv_in_working_directory_is_loaded(clean):
    (clean / ".env").write_text("CRUCIBLE_TEST_SECRET=local\n")
    assert general_utils.get_secret("CRUCIBLE_TEST_SECRET") == "local"


def test_explicit_env_file_must_exist(clean, monkeypatch):
    monkeypatch.setenv("DB_CONF_FILE", str(clean / "missing.env"))
    with pytest.raises(ConfigurationError, match="missing.env"):
        general_utils.get_secret("CRUCIBLE_TEST_SECRET")


def test_value_already_set_needs_no_env_file(clean, monkeypatch):
    monkeypatch.setenv("DB_CONF_FILE", str(clean / "missing.env"))
    monkeypatch.setenv("CRUCIBLE_TEST_SECRET", "set")
    assert general_utils.get_secret("CRUCIBLE_TEST_SECRET") == "set"


def test_explicit_file_is_not_mixed_with_working_directory_dotenv(clean, monkeypatch):
    explicit = clean / "staging.env"
    explicit.write_text("CRUCIBLE_TEST_SECRET_VERSION=" + VERSION + "\n")
    (clean / ".env").write_text("CRUCIBLE_TEST_SECRET=local\n")
    monkeypatch.setattr(general_utils, "get_gcs_secret", lambda name: "from-manager")
    config.load_env_file(str(explicit))
    assert general_utils.get_secret("CRUCIBLE_TEST_SECRET") == "from-manager"

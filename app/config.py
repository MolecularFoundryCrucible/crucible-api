import os
import re
from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict, SettingsError

DEFAULT_SECRETS_DIR = "/run/secrets"
_SECRET_VERSION = re.compile(r"projects/[A-Za-z0-9_-]+/secrets/[A-Za-z0-9_-]+/versions/(?:[1-9][0-9]*|latest)")
_REQUIRED = ("db_server", "db_username", "db_database")
_REQUIRED_SECRETS = ("pgdb_pw", "crucible_secret_key")
_FEATURE_REQUIREMENTS = {
    "ingestion_enabled": (("rmq_host", "rmq_user", "rmq_routing_suffix"), ("rabbitmq_default_pw",)),
    "printing_enabled": (("mqtt_host", "mqtt_username"), ("mqtt_password",)),
    "proposal_sync_enabled": (("proposal_db_url",), ("propdb_apikey",)),
}
_loaded_env_files: set[str] = set()
_fetched_secrets: dict[str, str] = {}


class ConfigurationError(RuntimeError):
    pass


class FeatureDisabled(Exception):
    def __init__(self, feature: str):
        super().__init__(f"{feature} is not enabled on this server")
        self.feature = feature


_FEATURE_FLAGS = {"ingestion": "ingestion_enabled", "printing": "printing_enabled", "proposal_sync": "proposal_sync_enabled"}


def feature_disabled_response(feature: str) -> dict:
    return {503: {"description": f"{feature.replace('_', ' ').capitalize()} is not enabled on this server: `{{\"detail\": {{\"code\": \"feature_disabled\", \"feature\": \"{feature}\", \"message\": ...}}}}`"}}


def require_feature(feature: str) -> None:
    if not getattr(get_settings(), _FEATURE_FLAGS[feature]):
        raise FeatureDisabled(feature)


class Settings(BaseSettings):
    """Settings read from environment variables, then from files in the secrets directory
    (one file per setting, named after it, as Docker and Kubernetes mount secrets).
    Values are trimmed. An empty value is ignored; a value of only spaces becomes empty, which
    get_secret and the startup check treat as unset and which flags reject."""

    model_config = SettingsConfigDict(case_sensitive=False, extra="ignore", env_ignore_empty=True, str_strip_whitespace=True)

    db_server: str | None = None
    db_username: str | None = None
    db_database: str | None = None
    pgdb_pw: SecretStr | None = None
    crucible_secret_key: SecretStr | None = None

    ingestion_enabled: bool = False
    rmq_host: str | None = None
    rmq_port: int = 5672
    rmq_user: str | None = None
    rmq_routing_suffix: str | None = None
    rabbitmq_default_pw: SecretStr | None = None

    printing_enabled: bool = False
    mqtt_host: str | None = None
    mqtt_port: int = 8883
    mqtt_username: str | None = None
    mqtt_password: SecretStr | None = None

    proposal_sync_enabled: bool = False
    proposal_db_url: str | None = None
    propdb_apikey: SecretStr | None = None

    upload_bucket: str | None = None
    gcs_project: str | None = None
    gcs_sa: SecretStr | None = None
    orcid_client_secret: SecretStr | None = None


def optional_env(name: str) -> str | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return None
    return value.strip()


def load_env_file(path: str | None = None) -> None:
    """Load KEY=value pairs into the environment without overriding it, for local runs: the named
    file, else DB_CONF_FILE, else ./.env. Never searches parent directories, so a checkout nested in
    another (a git worktree) cannot pick up the outer checkout's credentials. Once any file is
    loaded, calls without a path do nothing, so a later default lookup cannot mix in a different file."""
    from dotenv import load_dotenv

    if path is None and _loaded_env_files:
        return
    explicit = path or optional_env("DB_CONF_FILE")
    if explicit is not None:
        if not Path(explicit).is_file():
            raise ConfigurationError(f"Environment file {explicit} does not exist")
        target = explicit
    elif Path(".env").is_file():
        target = ".env"
    else:
        return
    resolved = str(Path(target).resolve())
    if resolved not in _loaded_env_files:
        load_dotenv(target, override=False)
        _loaded_env_files.add(resolved)
        get_settings.cache_clear()


def _secrets_dir() -> str | None:
    configured = optional_env("CRUCIBLE_SECRETS_DIR")
    if configured is not None:
        if not Path(configured).is_dir():
            raise ConfigurationError(f"CRUCIBLE_SECRETS_DIR {configured} is not a directory")
        return configured
    return DEFAULT_SECRETS_DIR if Path(DEFAULT_SECRETS_DIR).is_dir() else None


@lru_cache
def get_settings() -> Settings:
    load_env_file()
    try:
        return Settings(_secrets_dir=_secrets_dir())
    except ValidationError as error:
        problems = ", ".join(f"{str(issue['loc'][0]).upper()} ({issue['msg']})" for issue in error.errors())
        raise ConfigurationError(f"Invalid settings: {problems}") from None
    except (SettingsError, OSError, UnicodeDecodeError) as error:
        raise ConfigurationError(f"Unreadable file in the secrets directory: {error}") from None


def _secret_value(field: str) -> str | None:
    value = getattr(get_settings(), field)
    if value is None or not value.get_secret_value():
        return None
    return value.get_secret_value()


def get_secret(name: str) -> str:
    """Return a secret setting by its environment variable name. When the environment and the
    secrets directory do not provide it, fall back to the Secret Manager version in `{name}_VERSION`
    (a full projects/.../versions/... path), fetched once and kept in memory. The fallback serves
    deployments that still let the application fetch its own secrets."""
    field, name = name.lower(), name.upper()
    if Settings.model_fields.get(field) is None or Settings.model_fields[field].annotation != SecretStr | None:
        raise KeyError(f"{name} is not a secret setting")
    value = _secret_value(field)
    if value is not None:
        return value
    version = optional_env(f"{name}_VERSION")
    if version is None:
        raise ConfigurationError(f"{name} is not set (set {name}, a {name} file in the secrets directory, or {name}_VERSION)")
    if not _SECRET_VERSION.fullmatch(version):
        raise ConfigurationError(f"{name}_VERSION must be a full projects/<project>/secrets/<secret>/versions/<number or latest> path")
    if version not in _fetched_secrets:
        fetched = get_gcs_secret(version)
        if not fetched.strip():
            raise ConfigurationError(f"{name}_VERSION names an empty secret")
        _fetched_secrets[version] = fetched
    return _fetched_secrets[version]


def get_gcs_secret(secret_name: str) -> str:
    from google.cloud import secretmanager

    with secretmanager.SecretManagerServiceClient() as client:
        response = client.access_secret_version(name=secret_name)
    return response.payload.data.decode("UTF-8")


def missing_required_settings() -> list[str]:
    settings = get_settings()

    def has_version(field: str) -> bool:
        version = optional_env(f"{field.upper()}_VERSION")
        return version is not None and _SECRET_VERSION.fullmatch(version) is not None

    def missing(fields, secrets=False):
        if secrets:
            return [field for field in fields if _secret_value(field) is None and not has_version(field)]
        return [field for field in fields if not getattr(settings, field)]

    found = missing(_REQUIRED) + missing(_REQUIRED_SECRETS, secrets=True)
    for flag, (plain, secrets) in _FEATURE_REQUIREMENTS.items():
        if getattr(settings, flag):
            found += missing(plain) + missing(secrets, secrets=True)
    storage_requested = optional_env("STORAGE_READ_BUCKETS") or (optional_env("MULTIPART_UPLOAD_ENABLED") or "").lower() == "true"
    if storage_requested or settings.upload_bucket:
        found += missing(("upload_bucket",)) + missing(("gcs_sa",), secrets=True)
    return sorted(field.upper() for field in set(found))


def check_settings() -> None:
    found = missing_required_settings()
    if found:
        raise ConfigurationError(f"Missing required settings: {', '.join(found)}")

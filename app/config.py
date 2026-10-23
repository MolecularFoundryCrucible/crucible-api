import os
from functools import lru_cache
from pathlib import Path
from typing import get_args

from pydantic import SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict, SettingsError

DEFAULT_SECRETS_DIR = "/run/secrets"
_ALWAYS_REQUIRED = ("DB_SERVER", "DB_USERNAME", "DB_DATABASE")
_ALWAYS_REQUIRED_SECRETS = ("PGDB_PW", "CRUCIBLE_SECRET_KEY")
_FEATURE_REQUIREMENTS = {
    "INGESTION_ENABLED": (("RMQ_HOST", "RMQ_USER", "RMQ_ROUTING_SUFFIX"), ("RABBITMQ_DEFAULT_PW",)),
    "PRINTING_ENABLED": (("MQTT_HOST", "MQTT_USERNAME"), ("MQTT_PASSWORD",)),
    "PROPOSAL_SYNC_ENABLED": (("PROPOSAL_DB_URL",), ("PROPDB_APIKEY",)),
}


class ConfigurationError(RuntimeError):
    pass


class _BlankIsUnset(PydanticBaseSettingsSource):
    """A source wrapper that drops blank values, so a blank environment variable lets the
    secrets directory or the default apply instead of overriding them."""

    def __init__(self, settings_cls, source):
        super().__init__(settings_cls)
        self._source = source

    def get_field_value(self, field, field_name):
        return None, field_name, False

    def __call__(self):
        return {key: value for key, value in self._source().items() if not (isinstance(value, str) and not value.strip())}


class Settings(BaseSettings):
    """Settings read from environment variables, then from files in the secrets directory
    (one file per setting, named after it, as Docker and Kubernetes mount secrets)."""

    model_config = SettingsConfigDict(case_sensitive=False, extra="ignore", env_ignore_empty=True)

    db_server: str | None = None
    db_username: str | None = None
    db_database: str | None = None
    pgdb_pw: SecretStr | None = None
    crucible_secret_key: SecretStr | None = None

    ingestion_enabled: bool = False
    rmq_host: str | None = None
    rmq_user: str | None = None
    rmq_routing_suffix: str | None = None
    rabbitmq_default_pw: SecretStr | None = None

    printing_enabled: bool = False
    mqtt_host: str | None = None
    mqtt_username: str | None = None
    mqtt_password: SecretStr | None = None

    proposal_sync_enabled: bool = False
    proposal_db_url: str | None = None
    propdb_apikey: SecretStr | None = None

    gcs_sa: SecretStr | None = None
    orcid_client_secret: SecretStr | None = None

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings):
        return (init_settings, _BlankIsUnset(settings_cls, env_settings), _BlankIsUnset(settings_cls, file_secret_settings))

    @field_validator("*", mode="before")
    @classmethod
    def _strip_plain_values(cls, value, info):
        if isinstance(value, str) and SecretStr not in get_args(cls.model_fields[info.field_name].annotation):
            return value.strip()
        return value


_loaded_env_files: set[str] = set()


def optional_env(name: str) -> str | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return None
    return value.strip()


def load_env_file(path: str | None = None) -> None:
    """Load KEY=value pairs into the environment without overriding it: the named file, else
    DB_CONF_FILE, else ./.env. Never searches parent directories, so a checkout nested in another
    (a git worktree) cannot pick up the outer checkout's credentials. Once any file is loaded,
    calls without a path do nothing, so a later default lookup cannot mix in a different file."""
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


def is_secret_setting(name: str) -> bool:
    field = Settings.model_fields.get(name.lower())
    return field is not None and SecretStr in get_args(field.annotation)


def secret_file(name: str) -> str | None:
    """Contents of the secrets-directory file for a secret that is not a Settings field."""
    directory = _secrets_dir()
    if directory is None:
        return None
    for candidate in (name, name.lower()):
        path = Path(directory) / candidate
        if path.is_file():
            try:
                value = path.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError) as error:
                raise ConfigurationError(f"Unreadable file in the secrets directory: {error}") from None
            return value or None
    return None


def setting_value(name: str):
    """The configured value of a Settings field, by environment variable name."""
    field = name.lower()
    if field not in Settings.model_fields:
        raise KeyError(f"{name} is not a known setting")
    value = getattr(get_settings(), field)
    return value.get_secret_value() if isinstance(value, SecretStr) else value


def env_flag(name: str) -> bool:
    field = Settings.model_fields.get(name.lower())
    if field is None or field.annotation is not bool:
        raise KeyError(f"{name} is not a feature flag")
    return setting_value(name)


def secret_configured(name: str) -> bool:
    return setting_value(name) is not None or optional_env(f"{name}_VERSION") is not None


def missing_required_settings() -> list[str]:
    missing = [name for name in _ALWAYS_REQUIRED if setting_value(name) is None]
    missing += [name for name in _ALWAYS_REQUIRED_SECRETS if not secret_configured(name)]
    for flag, (settings, secrets) in _FEATURE_REQUIREMENTS.items():
        if env_flag(flag):
            missing += [name for name in settings if setting_value(name) is None]
            missing += [name for name in secrets if not secret_configured(name)]
    return sorted(set(missing))


def check_settings() -> None:
    missing = missing_required_settings()
    if missing:
        raise ConfigurationError(f"Missing required settings: {', '.join(missing)}")

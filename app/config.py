import os
from pathlib import Path

_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off"}
_ALWAYS_REQUIRED = ("DB_SERVER", "DB_USERNAME", "DB_DATABASE")
_ALWAYS_REQUIRED_SECRETS = ("PGDB_PW", "CRUCIBLE_SECRET_KEY")
_FEATURE_REQUIREMENTS = {
    "INGESTION_ENABLED": (("RMQ_HOST", "RMQ_USER", "RMQ_ROUTING_SUFFIX"), ("RABBITMQ_DEFAULT_PW",)),
    "PRINTING_ENABLED": (("MQTT_HOST", "MQTT_USERNAME"), ("MQTT_PASSWORD",)),
    "PROPOSAL_SYNC_ENABLED": (("PROPOSAL_DB_URL",), ("PROPDB_APIKEY",)),
}


class ConfigurationError(RuntimeError):
    pass


_loaded_env_files: set[str] = set()


def optional_env(name: str) -> str | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return None
    return value.strip()


def env_flag(name: str, default: bool = False) -> bool:
    value = optional_env(name)
    if value is None:
        return default
    if value.lower() in _TRUE:
        return True
    if value.lower() in _FALSE:
        return False
    raise ConfigurationError(f"{name} must be true or false, got {value!r}")


def secret_configured(name: str) -> bool:
    return any(optional_env(f"{name}{suffix}") for suffix in ("", "_FILE", "_VERSION"))


def missing_required_settings() -> list[str]:
    load_env_file()
    missing = [name for name in _ALWAYS_REQUIRED if optional_env(name) is None]
    missing += [name for name in _ALWAYS_REQUIRED_SECRETS if not secret_configured(name)]
    for flag, (settings, secrets) in _FEATURE_REQUIREMENTS.items():
        if env_flag(flag):
            missing += [name for name in settings if optional_env(name) is None]
            missing += [name for name in secrets if not secret_configured(name)]
    return sorted(set(missing))


def check_settings() -> None:
    missing = missing_required_settings()
    if missing:
        raise ConfigurationError(f"Missing required settings: {', '.join(missing)}")


def load_env_file(path: str | None = None) -> None:
    """Load KEY=value pairs without overriding the environment: the named file, else DB_CONF_FILE,
    else ./.env. Never searches parent directories, so a checkout nested in another (a git worktree)
    cannot pick up the outer checkout's credentials. Once any file is loaded, calls without a path
    do nothing, so a later default lookup cannot mix in a different file."""
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

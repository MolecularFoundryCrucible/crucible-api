import os
import re
from urllib.parse import urlparse
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, ValidationError, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict, SettingsError

from app.utils.multipart_config import MultipartStorageConfig

DEFAULT_SECRETS_DIR = "/run/secrets"
_SECRET_VERSION = re.compile(r"projects/[A-Za-z0-9_-]+/secrets/[A-Za-z0-9_-]+/versions/(?:[1-9][0-9]*|latest)")
_loaded_env_files: set[str] = set()
_fetched_secrets: dict[str, str] = {}


class ConfigurationError(RuntimeError):
    pass


class FeatureDisabled(Exception):
    def __init__(self, feature: str):
        super().__init__(f"{feature} is not enabled on this server")
        self.feature = feature


def optional_env(name: str) -> str | None:
    """A trimmed environment variable, matched regardless of case like the settings; None when
    unset or blank."""
    value = os.environ.get(name)
    if value is None:
        value = next((found for key, found in os.environ.items() if key.upper() == name.upper()), None)
    if value is None or not value.strip():
        return None
    return value.strip()


def _is_secret(group: type[BaseSettings], field: str) -> bool:
    return group.model_fields[field].annotation == SecretStr | None


def _read_pointed_file(name: str, path: str) -> str:
    try:
        value = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ConfigurationError(f"{name}_FILE names a file that cannot be read ({path}): {error.__class__.__name__}") from None
    if not value.strip():
        raise ConfigurationError(f"{name}_FILE names an empty file ({path})")
    return value


def _by_upper_name(items) -> tuple[dict, set[str]]:
    found, duplicated = {}, set()
    for key, value in items:
        if key.upper() in found:
            duplicated.add(key.upper())
        found[key.upper()] = value
    return found, duplicated


def _read_secrets_file(name: str, path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ConfigurationError(f"The {name} file in the secrets directory cannot be read ({path}): {error.__class__.__name__}") from None


class SecretSources(PydanticBaseSettingsSource):
    """Reads each setting from exactly one of: a file named by NAME_FILE, a file NAME in the secrets
    directory, or the NAME environment variable. For secrets, NAME_VERSION (fetched later by
    get_secret) also counts as a way. Names match regardless of case; a blank value (empty or only
    spaces) counts as not given. A setting given more than one way is an error."""

    def __init__(self, settings_cls: type[BaseSettings], secrets_dir: str | None):
        super().__init__(settings_cls)
        self.secrets_dir = Path(secrets_dir) if secrets_dir else None

    def get_field_value(self, field, field_name):
        raise NotImplementedError

    def __call__(self) -> dict[str, str]:
        environment, duplicated = _by_upper_name(os.environ.items())
        paths = (path for path in self.secrets_dir.iterdir() if path.is_file()) if self.secrets_dir else ()
        files, duplicated_files = _by_upper_name((path.name, path) for path in paths)
        values = {}
        for field, info in self.settings_cls.model_fields.items():
            name = self.settings_cls.env_name(field)
            secret = _is_secret(self.settings_cls, field)
            related = {name, f"{name}_FILE"} | ({f"{name}_VERSION"} if secret else set())
            if related & duplicated or name in duplicated_files:
                raise ConfigurationError(f"{name} is given more than once with names that differ only in case; give it one way only")
            ways: dict[str, str | None] = {}
            if environment.get(name, "").strip():
                ways[name] = environment[name]
            pointer = environment.get(f"{name}_FILE", "").strip()
            if pointer:
                ways[f"{name}_FILE"] = _read_pointed_file(name, pointer)
            if name in files:
                content = _read_secrets_file(name, files[name])
                if content.strip():
                    ways[f"a {name} file in the secrets directory"] = content
            if secret and environment.get(f"{name}_VERSION", "").strip():
                ways[f"{name}_VERSION"] = None
            if len(ways) > 1:
                raise ConfigurationError(f"{name} is given more than one way ({', '.join(ways)}); give it one way only")
            for value in ways.values():
                if value is not None:
                    values[info.validation_alias if isinstance(info.validation_alias, str) else field] = value.strip()
        return values


class SettingsGroup(BaseSettings):
    """One area of settings. Each setting is read from exactly one of NAME_FILE, a file NAME in the
    secrets directory (as Docker and Kubernetes mount secrets), or the NAME environment variable.
    Values are trimmed, and a blank value counts as not given."""

    model_config = SettingsConfigDict(case_sensitive=False, extra="ignore", env_ignore_empty=True, str_strip_whitespace=True)

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings):
        return (init_settings, SecretSources(settings_cls, file_secret_settings.secrets_dir))

    @classmethod
    def env_name(cls, field: str) -> str:
        alias = cls.model_fields[field].validation_alias
        if isinstance(alias, str):
            return alias.upper()
        return (cls.model_config.get("env_prefix", "") + field).upper()

    def missing(self, *fields: str) -> list[str]:
        """Environment names of the given fields that have no value (secrets may also be given as a
        Secret Manager version)."""
        found = []
        for field in fields:
            value = getattr(self, field)
            if isinstance(value, SecretStr):
                value = value.get_secret_value()
            if value:
                continue
            name = self.env_name(field)
            version = optional_env(f"{name}_VERSION")
            if _is_secret(type(self), field) and version is not None and _SECRET_VERSION.fullmatch(version):
                continue
            found.append(name)
        return found

    def problems(self, settings: "Settings") -> list[str]:
        return []

    def in_use(self) -> bool:
        return getattr(self, "enabled", True)


class DatabaseSettings(SettingsGroup):
    model_config = SettingsConfigDict(env_prefix="DB_")

    server: str | None = None
    username: str | None = None
    database: str | None = None
    password: SecretStr | None = Field(None, validation_alias="PGDB_PW")
    pool_size: int = 15
    max_overflow: int = 15
    pool_timeout: int = 30
    pool_recycle: int = 1800
    expected_revisions: str | None = Field(None, validation_alias="EXPECTED_DB_REVISIONS")

    def problems(self, settings: "Settings") -> list[str]:
        return self.missing("server", "username", "database", "password")


class SecuritySettings(SettingsGroup):
    model_config = SettingsConfigDict(env_prefix="CRUCIBLE_")

    secret_key: SecretStr | None = None
    cursor_signing_key: SecretStr | None = None
    cursor_signing_keys: SecretStr | None = None
    cursor_active_key_id: str = "v1"

    def problems(self, settings: "Settings") -> list[str]:
        return self.missing("secret_key")


class ApiSettings(SettingsGroup):
    model_config = SettingsConfigDict(env_prefix="API_")

    public_url: str | None = None
    path: str | None = None
    conf_file: str | None = None
    trgm_search_threshold: float | None = Field(None, validation_alias="TRGM_SEARCH_THRESHOLD")
    orcid_client_id: str | None = Field(None, validation_alias="ORCID_CLIENT_ID")
    orcid_issuer: str = Field("https://orcid.org", validation_alias="ORCID_ISSUER")
    orcid_client_secret: SecretStr | None = Field(None, validation_alias="ORCID_CLIENT_SECRET")


class HubSettings(SettingsGroup):
    model_config = SettingsConfigDict(env_prefix="HUB_AUTH_")

    enabled: bool = False
    client_id: str = "crucible-hub"
    orcid_client_id: str | None = None
    orcid_issuer: str = "https://orcid.org"
    callback_uri: str = ""
    public_api_url: str | None = None
    client_secret: SecretStr | None = None
    orcid_client_secret: SecretStr | None = None

    def problems(self, settings: "Settings") -> list[str]:
        if not self.enabled:
            return []
        found = self.missing("client_secret", "callback_uri")
        if self.orcid_client_secret is not None or optional_env("HUB_AUTH_ORCID_CLIENT_SECRET_VERSION") is not None:
            found += self.missing("orcid_client_secret")
        elif settings.api.missing("orcid_client_secret"):
            found.append("HUB_AUTH_ORCID_CLIENT_SECRET or ORCID_CLIENT_SECRET")
        return found


class StorageSettings(SettingsGroup):
    backend: Literal["gcs", "s3"] = Field("gcs", validation_alias="STORAGE_BACKEND")
    upload_bucket: str | None = Field(None, validation_alias="UPLOAD_BUCKET")
    gcs_project: str | None = Field(None, validation_alias="GCS_PROJECT")
    record_bucket: str | None = Field(None, validation_alias="RECORD_BUCKET")
    read_buckets: str | None = Field(None, validation_alias="STORAGE_READ_BUCKETS")
    legacy_upload_token_downscope: bool = Field(True, validation_alias="LEGACY_UPLOAD_TOKEN_DOWNSCOPE")
    s3_endpoint_url: str | None = Field(None, validation_alias="S3_ENDPOINT_URL")
    s3_public_url: str | None = Field(None, validation_alias="S3_PUBLIC_URL")
    s3_region: str = Field("us-east-1", validation_alias="S3_REGION")
    s3_access_key_id: SecretStr | None = Field(None, validation_alias="S3_ACCESS_KEY_ID")
    s3_secret_access_key: SecretStr | None = Field(None, validation_alias="S3_SECRET_ACCESS_KEY")
    file_version_retention_days: int = Field(14, ge=1, validation_alias="FILE_VERSION_RETENTION_DAYS")
    cleanup_enabled: bool = Field(False, validation_alias="STORAGE_CLEANUP_ENABLED")

    @field_validator("s3_endpoint_url")
    @classmethod
    def not_google(cls, value: str | None, info: ValidationInfo) -> str | None:
        host = urlparse(value or "").hostname or ""
        if info.data.get("backend") == "s3" and (host == "googleapis.com" or host.endswith(".googleapis.com")):
            raise ValueError("points at Google Cloud Storage, whose S3 interface refuses the checksums S3 uploads send; use STORAGE_BACKEND=gcs")
        return value

    @property
    def read_bucket_names(self) -> set[str]:
        return {name.strip() for name in (self.read_buckets or "").split(",")} - {""}

    def serves_files(self) -> bool:
        multipart = (optional_env("MULTIPART_UPLOAD_ENABLED") or "").lower() == "true"
        return bool(self.read_buckets or multipart or self.upload_bucket)

    def in_use(self) -> bool:
        return self.serves_files() or bool(self.record_bucket)

    def problems(self, settings: "Settings") -> list[str]:
        if not self.in_use():
            return []
        credentials = ("s3_endpoint_url", "s3_access_key_id", "s3_secret_access_key") if self.backend == "s3" else ()
        return self.missing("upload_bucket", *credentials) if self.serves_files() else self.missing(*credentials)


class IngestionSettings(SettingsGroup):
    model_config = SettingsConfigDict(env_prefix="RMQ_")

    enabled: bool = Field(False, validation_alias="INGESTION_ENABLED")
    host: str | None = None
    port: int = 5672
    user: str | None = None
    routing_suffix: str | None = None
    password: SecretStr | None = Field(None, validation_alias="RABBITMQ_DEFAULT_PW")

    def problems(self, settings: "Settings") -> list[str]:
        return self.missing("host", "user", "routing_suffix", "password") if self.enabled else []


class PrintingSettings(SettingsGroup):
    model_config = SettingsConfigDict(env_prefix="MQTT_")

    enabled: bool = Field(False, validation_alias="PRINTING_ENABLED")
    host: str | None = None
    port: int = 8883
    username: str | None = None
    password: SecretStr | None = None

    def problems(self, settings: "Settings") -> list[str]:
        return self.missing("host", "username", "password") if self.enabled else []


class ProposalSettings(SettingsGroup):
    enabled: bool = Field(False, validation_alias="PROPOSAL_SYNC_ENABLED")
    url: str | None = Field(None, validation_alias="PROPOSAL_DB_URL")
    api_key: SecretStr | None = Field(None, validation_alias="PROPDB_APIKEY")

    def problems(self, settings: "Settings") -> list[str]:
        return self.missing("url", "api_key") if self.enabled else []


class Settings(BaseModel):
    db: DatabaseSettings
    security: SecuritySettings
    api: ApiSettings
    hub: HubSettings
    storage: StorageSettings
    ingestion: IngestionSettings
    printing: PrintingSettings
    proposals: ProposalSettings


GROUPS: dict[str, type[SettingsGroup]] = {name: field.annotation for name, field in Settings.model_fields.items()}
_SECRETS: dict[str, tuple[str, str]] = {
    group.env_name(field): (name, field)
    for name, group in GROUPS.items()
    for field, info in group.model_fields.items()
    if info.annotation == SecretStr | None
}
# Sent verbatim in HTTP Basic credentials and ORCID token requests, where a stray newline breaks every sign-in.
_SINGLE_LINE_SECRETS = frozenset({"HUB_AUTH_CLIENT_SECRET", "HUB_AUTH_ORCID_CLIENT_SECRET", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"})
_FEATURES = {"ingestion": "ingestion", "printing": "printing", "proposal_sync": "proposals"}


def require_feature(feature: str) -> None:
    if not getattr(get_settings(), _FEATURES[feature]).enabled:
        raise FeatureDisabled(feature)


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
    secrets_dir = _secrets_dir()
    groups, problems = {}, []
    for name, group in GROUPS.items():
        try:
            groups[name] = group(_secrets_dir=secrets_dir)
        except ValidationError as error:
            for issue in error.errors():
                location = str(issue["loc"][0])
                label = group.env_name(location) if location in group.model_fields else location.upper()
                problems.append(f"{label} ({issue['msg']})")
        except (SettingsError, OSError, UnicodeDecodeError) as error:
            raise ConfigurationError(f"Unreadable file in the secrets directory: {error.__class__.__name__}") from None
    if problems:
        raise ConfigurationError(f"Invalid settings: {', '.join(problems)}")
    return Settings(**groups)


def configured_secret(name: str) -> str | None:
    """A secret setting given as NAME_FILE, a file in the secrets directory, or NAME, without
    Secret Manager; None when it is not given that way."""
    name = name.upper()
    if name not in _SECRETS:
        raise KeyError(f"{name} is not a secret setting")
    group, field = _SECRETS[name]
    value = getattr(getattr(get_settings(), group), field)
    return value.get_secret_value() if value is not None and value.get_secret_value() else None


def optional_secret(name: str) -> str | None:
    """A secret that may be left out: its value when given any way, including NAME_VERSION; None
    when it is not given at all."""
    name = name.upper()
    if configured_secret(name) is None and optional_env(f"{name}_VERSION") is None:
        return None
    return get_secret(name)


def get_secret(name: str) -> str:
    """Return a secret setting by its environment variable name. When it is not given as NAME_FILE,
    a file in the secrets directory, or NAME, fall back to the Secret Manager version in
    `{name}_VERSION` (a full projects/.../versions/... path), fetched once and kept in memory. The
    fallback serves the Foundry until its platform delivers the secrets. Every failure is a
    ConfigurationError that names the setting and never includes the value."""
    name = name.upper()
    value = configured_secret(name)
    if value is None:
        version = optional_env(f"{name}_VERSION")
        if version is None:
            raise ConfigurationError(f"{name} is not set (set {name}_FILE, a {name} file in the secrets directory, {name}, or {name}_VERSION)")
        if not _SECRET_VERSION.fullmatch(version):
            raise ConfigurationError(f"{name}_VERSION must be a full projects/<project>/secrets/<secret>/versions/<number or latest> path")
        if version not in _fetched_secrets:
            try:
                fetched = get_gcs_secret(version)
            except Exception as error:
                raise ConfigurationError(f"{name}_VERSION could not be read: {error.__class__.__name__}") from None
            if not fetched.strip():
                raise ConfigurationError(f"{name}_VERSION names an empty secret")
            _fetched_secrets[version] = fetched
        value = _fetched_secrets[version]
    if name in _SINGLE_LINE_SECRETS and not (value.isascii() and value.isprintable()):
        raise ConfigurationError(f"{name} must be one line of printable ASCII")
    return value


def get_gcs_secret(secret_name: str) -> str:
    from google.cloud import secretmanager

    with secretmanager.SecretManagerServiceClient() as client:
        response = client.access_secret_version(request={"name": secret_name}, timeout=10, retry=None)
    return response.payload.data.decode("UTF-8")


def missing_required_settings() -> list[str]:
    settings = get_settings()
    return sorted({name for group in GROUPS for name in getattr(settings, group).problems(settings)})


def check_settings() -> None:
    found = missing_required_settings()
    if found:
        raise ConfigurationError(f"Missing required settings: {', '.join(found)}")
    try:
        MultipartStorageConfig.from_env()
    except ValueError as error:
        raise ConfigurationError(f"Invalid multipart upload settings: {error}") from error


def secrets_in_use() -> list[str]:
    """The secrets a server reads at startup: every secret given any way for a feature in use."""
    settings = get_settings()
    return [
        name for name, (group, _field) in _SECRETS.items()
        if getattr(settings, group).in_use() and (configured_secret(name) is not None or optional_env(f"{name}_VERSION") is not None)
    ]


def read_secrets() -> None:
    """Read every secret given for a feature in use, into the cache requests read from, so a worker
    that cannot read one fails to start instead of failing on live traffic."""
    for name in secrets_in_use():
        get_secret(name)

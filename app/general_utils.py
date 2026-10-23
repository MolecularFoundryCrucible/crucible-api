import os
import re
from datetime import datetime, timezone
import hashlib

def remove_file(filepath):
    os.remove(filepath)

def is_orcid(val):
    pattern= re.compile("[0-9A-Z]{4}-[0-9A-Z]{4}-[0-9A-Z]{4}-[0-9A-Z]{4}$")
    if re.match(pattern, val):
        return True
    else:
        return False

_MFID_PATTERN = re.compile(r"^[0-9a-hjkmnp-tv-z]{26}$")

def is_valid_mfid(val):
    """Check that val matches the mfid format: 26-character lowercase
    Crockford's Base32 (excludes i, l, o, u to avoid visual ambiguity)."""
    return bool(_MFID_PATTERN.match(val))


def blank_to_none(val):
    """Normalise a blank string to None for optional identifier fields.

    "" is a real value: it matches rows, satisfies foreign keys, and creates
    access groups. Uploaders sending project_id="" built a nameless project
    holding 1,292 datasets. Coerced rather than rejected because absent is
    legitimate here; fields where the value IS the identity must reject.
    """
    if isinstance(val, str) and not val.strip():
        return None
    return val


_SECRET_VERSION = re.compile(r"projects/[A-Za-z0-9_-]+/secrets/[A-Za-z0-9_-]+/versions/(?:[1-9][0-9]*|latest)")


def get_secret(name: str) -> str:
    """Return a secret from, in order: the environment variable `name`, the file named by
    `{name}_FILE` (Docker secrets), or the Secret Manager version named by `{name}_VERSION`
    (a full projects/.../versions/... path). Secret Manager values are cached in the environment."""
    from app.config import ConfigurationError, load_env_file, optional_env

    value = os.getenv(name)
    if value is not None and value.strip():
        return value
    load_env_file()
    value = os.getenv(name)
    if value is not None and value.strip():
        return value
    file_path = optional_env(f"{name}_FILE")
    if file_path is not None:
        try:
            with open(file_path, encoding="utf-8") as handle:
                value = handle.read().removesuffix("\n").removesuffix("\r")
        except (OSError, UnicodeDecodeError) as error:
            raise ConfigurationError(f"{name}_FILE points to an unreadable file: {error}") from None
        if not value.strip():
            raise ConfigurationError(f"{name}_FILE points to an empty file")
        return value
    version = optional_env(f"{name}_VERSION")
    if version is not None:
        if not _SECRET_VERSION.fullmatch(version):
            raise ConfigurationError(f"{name}_VERSION must be a full projects/<project>/secrets/<secret>/versions/<number or latest> path")
        value = get_gcs_secret(version)
        if not value.strip():
            raise ConfigurationError(f"{name}_VERSION names an empty secret")
        os.environ[name] = value
        return value
    raise ConfigurationError(f"{name} is not set (set {name}, {name}_FILE, or {name}_VERSION)")


def get_gcs_secret(secret_name: str) -> str:
    from google.cloud import secretmanager

    with secretmanager.SecretManagerServiceClient() as client:
        response = client.access_secret_version(name=secret_name)
    return response.payload.data.decode("UTF-8")


def get_utc_isoformat():
    return datetime.now(timezone.utc).isoformat()


def get_utc_datetime():
    return datetime.now(timezone.utc)


def checkhash(file):
    """Calculate SHA256 hash of a file.
    
    Args:
        file (str): Path to the file to hash
        
    Returns:
        str: Hexadecimal SHA256 hash of the file
    """
    with open(file,"rb") as f:
        fdata = f.read() 
        readable_hash = hashlib.sha256(fdata).hexdigest()
    return(readable_hash)


def if_match_satisfied(if_match, current_etag):
    if if_match is None or if_match.strip() == "*":
        return True
    current = current_etag.removeprefix("W/")
    return any(candidate.strip().removeprefix("W/") == current for candidate in if_match.split(","))

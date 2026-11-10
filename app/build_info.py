import os
import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def api_version() -> str:
    configured_version = os.getenv("API_VERSION")
    if configured_version:
        return configured_version
    try:
        return version("crucible-api")
    except PackageNotFoundError:
        try:
            with (Path(__file__).resolve().parents[1] / "pyproject.toml").open("rb") as file:
                return tomllib.load(file)["project"]["version"]
        except (FileNotFoundError, KeyError, tomllib.TOMLDecodeError):
            return "unknown"


def git_commit() -> str | None:
    return os.getenv("GITHASH") or None


def branch_name() -> str | None:
    return os.getenv("BRANCH_NAME") or None


def version_stamp() -> tuple[str, str | None]:
    """The API version and commit that each record row is stamped with when it is written."""
    return f"v{api_version()}", git_commit()

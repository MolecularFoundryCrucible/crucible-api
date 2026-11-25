import re
from typing import Annotated, Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.models.ingestion import INGESTION_CLASS_PATTERN
from app.models.timestamps import UtcDatetime

WORKER_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
BUCKET_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,221}$"
EXTENSION_PATTERN = r"^\.[A-Za-z0-9][A-Za-z0-9._-]{0,31}$"
WorkerKind = Literal["ingestion"]


MAX_DESCRIPTION = 2000
MAX_EXTENSIONS = 50


class Capability(BaseModel):
    """One thing a worker can do: for kind ingestion, an ingestor."""
    name: str
    description: Optional[str] = None
    file_extensions: list[str] = Field(default_factory=list, description="File name endings it handles, with the leading dot, such as .mrc or .tar.gz; compared without case")


class IgnoredEntry(BaseModel):
    name: Optional[str] = Field(None, description="The capability the entry belongs to")
    field: str
    reason: str


def clean_capabilities(raw: list[dict[str, Any]]) -> tuple[list[Capability], list[IgnoredEntry]]:
    """Keep what is valid of each capability and report the rest, so one bad entry does not cost the
    worker its registration: a bad or repeated name drops that capability, a bad field only that field.
    Extensions are lowercased and given their leading dot."""
    kept, ignored, seen = [], [], set()
    for entry in raw:
        name = entry.get("name")
        label = name if isinstance(name, str) else None
        if not isinstance(name, str) or not re.fullmatch(INGESTION_CLASS_PATTERN, name):
            ignored.append(IgnoredEntry(name=label, field="name", reason="not a valid ingestor name"))
            continue
        if name in seen:
            ignored.append(IgnoredEntry(name=name, field="name", reason="listed more than once"))
            continue
        seen.add(name)
        description = entry.get("description")
        if description is not None and not isinstance(description, str):
            ignored.append(IgnoredEntry(name=name, field="description", reason="not a string"))
            description = None
        elif description is not None and len(description) > MAX_DESCRIPTION:
            ignored.append(IgnoredEntry(name=name, field="description", reason=f"longer than {MAX_DESCRIPTION} characters"))
            description = None
        extensions = set()
        listed = entry.get("file_extensions") or []
        if not isinstance(listed, list):
            ignored.append(IgnoredEntry(name=name, field="file_extensions", reason="not a list"))
            listed = []
        for extension in listed:
            normal = extension.strip().lower() if isinstance(extension, str) else None
            if normal and not normal.startswith("."):
                normal = "." + normal
            if normal is None or not re.fullmatch(EXTENSION_PATTERN, normal):
                ignored.append(IgnoredEntry(name=name, field="file_extensions", reason=f"not a file extension: {extension!r}"))
            elif len(extensions) >= MAX_EXTENSIONS:
                ignored.append(IgnoredEntry(name=name, field="file_extensions", reason=f"more than {MAX_EXTENSIONS}: {extension!r}"))
            else:
                extensions.add(normal)
        for key in sorted(set(entry) - {"name", "description", "file_extensions"}):
            ignored.append(IgnoredEntry(name=name, field=key, reason="unknown field"))
        kept.append(Capability(name=name, description=description, file_extensions=sorted(extensions)))
    return kept, ignored


class IngestorDetail(BaseModel):
    name: str
    description: Optional[str] = None
    file_extensions: list[str] = Field(default_factory=list)


class WorkerRegister(BaseModel):
    """What a worker can do. capabilities: for kind ingestion, the ingestors it runs. buckets: the buckets it can read."""
    model_config = ConfigDict(extra="forbid")

    kind: WorkerKind
    capabilities: list[dict[str, Any]] = Field(default_factory=list, max_length=500, description="One object per capability: name, and optionally description (up to 2000 characters) and file_extensions (up to 50, such as .mrc). Invalid parts are dropped and listed in the response's ignored, not refused")
    buckets: list[Annotated[str, StringConstraints(pattern=BUCKET_PATTERN)]] = Field(default_factory=list, max_length=50)
    worker_version: Optional[str] = Field(None, max_length=128)
    package_version: Optional[str] = Field(None, max_length=128, description="For kind ingestion, the crucible-ingestion version")



class WorkerRead(BaseModel):
    worker_id: str
    kind: str
    capabilities: list[Capability]
    buckets: list[str]
    store_ids: list[str] = Field(description="The stores whose bucket the worker reads")
    worker_version: Optional[str] = None
    package_version: Optional[str] = None
    registered_at: UtcDatetime
    last_seen_at: UtcDatetime
    expires_at: UtcDatetime
    live: bool


class WorkerRegistered(WorkerRead):
    heartbeat_seconds: int = Field(description="Renew the registration this often; it expires at expires_at")
    ignored: list[IgnoredEntry] = Field(default_factory=list, description="Parts of the capabilities that were invalid and left out; log them")


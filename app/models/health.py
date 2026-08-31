from typing import Literal

from pydantic import BaseModel


class LivenessResponse(BaseModel):
    status: Literal["ok"]


class BuildInfoResponse(BaseModel):
    api_version: str
    git_commit: str | None
    branch: str | None


class DatabaseReadinessResponse(BaseModel):
    status: Literal["ok", "error"]
    latency_ms: float | None
    schema_revisions: list[str]


class ReadinessResponse(BaseModel):
    status: Literal["ok", "degraded"]
    build: BuildInfoResponse
    database: DatabaseReadinessResponse

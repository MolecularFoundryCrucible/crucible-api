from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field
from app.models.timestamps import UtcDatetime

PostprocessingRunStatus = Literal["requested", "running", "completed", "failed"]


class PipelineRead(BaseModel):
    name: str
    description: str
    parameters_schema: dict[str, Any]


class PostprocessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pipeline: str = Field(description="Name of a pipeline from GET /pipelines.")
    parameters: dict[str, Any] = Field(default_factory=dict, description="Pipeline parameters, validated against the pipeline's parameters_schema.")


class PostprocessingRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    run_id: int = Field(validation_alias="id")
    dataset_mfid: str
    pipeline: str
    parameters: dict[str, Any]
    status: PostprocessingRunStatus
    error: Optional[str] = None
    requested_by: Optional[str] = None
    requested_at: UtcDatetime
    started_at: Optional[UtcDatetime] = None
    finished_at: Optional[UtcDatetime] = None


class PostprocessingRunUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["running", "completed", "failed"]
    error: Optional[str] = Field(default=None, max_length=4000)

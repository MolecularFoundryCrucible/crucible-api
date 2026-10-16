from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, computed_field


class ChunkSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query_vector: list[FiniteFloat] = Field(
        min_length=768,
        max_length=768,
        description="Query embedding; it must match the table's 768 dimensions.",
    )
    k: int = Field(default=20, ge=1, le=100)
    probes: int = Field(default=10, ge=1, le=32768)
    model_name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    resource_mfids: Optional[list[str]] = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="Optionally restrict results to these resource MFIDs.",
    )


class ResourceEmbeddingRead(BaseModel):
    id: int
    resource_mfid: str
    chunk_index: int
    chunk_text: str
    embedding: Optional[list[float]]
    model_name: str


class ChunkHit(BaseModel):
    resource_mfid: str
    chunk_index: int
    chunk_text: str
    distance: float

    @computed_field
    @property
    def similarity(self) -> float:
        return 1.0 - self.distance

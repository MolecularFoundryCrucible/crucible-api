from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

STORE_ID_PATTERN = r"^[a-z][a-z0-9]{0,31}$"
COMPATIBILITY_STORE_ID = "default"


class StoreConfig(BaseModel):
    """One entry of STORAGE_STORES: an object store the API manages. The id is stored on file rows,
    so it never changes once files use it."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=STORE_ID_PATTERN)
    display_name: str = Field(min_length=1)
    description: str | None = None
    type: Literal["gcs", "s3"]
    bucket: str = Field(min_length=1)
    endpoint_url: str | None = None
    public_url: str | None = None
    region: str = "us-east-1"
    project: str | None = None
    service_account: str | None = None
    _compatibility: bool = PrivateAttr(False)

    @model_validator(mode="after")
    def s3_needs_an_endpoint(self) -> "StoreConfig":
        if self.type == "s3" and not self.endpoint_url:
            raise ValueError(f"store {self.id} is s3 and needs endpoint_url")
        host = (self.endpoint_url or "").split("://")[-1].split("/")[0].split(":")[0]
        if self.type == "s3" and (host == "googleapis.com" or host.endswith(".googleapis.com")):
            raise ValueError(f"store {self.id} points at Google Cloud Storage, whose S3 interface refuses the checksums S3 uploads send; use type gcs")
        return self

    @classmethod
    def from_old_settings(cls, **values) -> "StoreConfig":
        """The single store today's STORAGE_BACKEND and bucket settings describe, unvalidated so an
        unconfigured server keeps answering storage_not_configured where it did."""
        store = cls.model_construct(id=COMPATIBILITY_STORE_ID, display_name="Default", description=None, **values)
        store._compatibility = True
        return store

    @property
    def compatibility(self) -> bool:
        return self._compatibility

    def _secret(self, suffix: str) -> str:
        return f"STORAGE_{self.id.upper()}_{suffix}"

    @property
    def access_key_name(self) -> str:
        return "S3_ACCESS_KEY_ID" if self.compatibility else self._secret("ACCESS_KEY_ID")

    @property
    def secret_key_name(self) -> str:
        return "S3_SECRET_ACCESS_KEY" if self.compatibility else self._secret("SECRET_ACCESS_KEY")

    @property
    def credentials_name(self) -> str | None:
        return None if self.compatibility else self._secret("CREDENTIALS")

    @property
    def secret_names(self) -> list[str]:
        if self.compatibility:
            return []
        return [self.access_key_name, self.secret_key_name] if self.type == "s3" else [self.credentials_name]

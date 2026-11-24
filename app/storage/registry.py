from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

STORE_ID_PATTERN = r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$"


class StoreConfig(BaseModel):
    """One entry of STORAGE_STORES: an object store the API manages. The id is stored on file rows,
    so it never changes once files use it."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=STORE_ID_PATTERN, max_length=63)
    display_name: str = Field(min_length=1)
    description: str | None = None
    type: Literal["gcs", "s3"]
    bucket: str = Field(min_length=1)
    read_buckets: tuple[str, ...] = ()
    endpoint_url: str | None = None
    public_url: str | None = None
    region: str = "us-east-1"
    project: str | None = None
    service_account: str | None = None

    @model_validator(mode="after")
    def s3_needs_an_endpoint(self) -> "StoreConfig":
        if self.type == "s3" and not self.endpoint_url:
            raise ValueError(f"store {self.id} is s3 and needs endpoint_url")
        host = (self.endpoint_url or "").split("://")[-1].split("/")[0].split(":")[0]
        if self.type == "s3" and (host == "googleapis.com" or host.endswith(".googleapis.com")):
            raise ValueError(f"store {self.id} points at Google Cloud Storage, whose S3 interface refuses the checksums S3 uploads send; use type gcs")
        if self.type == "gcs" and (self.endpoint_url or self.public_url):
            raise ValueError(f"store {self.id} is gcs and takes no endpoint_url or public_url")
        if self.type == "s3" and (self.project or self.service_account):
            raise ValueError(f"store {self.id} is s3 and takes no project or service_account")
        return self

    def _secret(self, suffix: str) -> str:
        return f"STORAGE_{self.id.upper().replace('-', '_')}_{suffix}"

    @property
    def access_key_name(self) -> str:
        return self._secret("ACCESS_KEY_ID")

    @property
    def secret_key_name(self) -> str:
        return self._secret("SECRET_ACCESS_KEY")

    @property
    def credentials_name(self) -> str:
        return self._secret("CREDENTIALS")

    @property
    def secret_names(self) -> list[str]:
        return [self.access_key_name, self.secret_key_name] if self.type == "s3" else [self.credentials_name]

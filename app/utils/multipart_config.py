import os
from dataclasses import dataclass
from collections.abc import Mapping


@dataclass(frozen=True)
class MultipartStorageConfig:
    enabled: bool = False
    bucket: str | None = None
    signer_service_account: str | None = None
    project: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "MultipartStorageConfig":
        env = os.environ if env is None else env
        flag = env.get("MULTIPART_UPLOAD_ENABLED", "false").strip().lower()
        if flag not in {"true", "false"}:
            raise ValueError("MULTIPART_UPLOAD_ENABLED must be true or false")
        config = cls(
            enabled=flag == "true",
            bucket=env.get("MULTIPART_UPLOAD_BUCKET", "").strip() or None,
            signer_service_account=env.get("MULTIPART_UPLOAD_SIGNER", "").strip() or None,
            project=env.get("MULTIPART_UPLOAD_PROJECT", "").strip() or None,
        )
        if config.enabled and not all((config.bucket, config.signer_service_account, config.project)):
            raise ValueError("Multipart uploads require an explicit bucket, signer, and project")
        if config.bucket == "mf-storage-prod":
            raise ValueError("Multipart uploads require a dedicated bucket")
        return config

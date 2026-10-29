import os
from dataclasses import dataclass
from collections.abc import Mapping


@dataclass(frozen=True)
class MultipartStorageConfig:
    enabled: bool = False
    bucket: str | None = None
    signer_service_account: str | None = None
    project: str | None = None

    max_upload_bytes: int = 500 * 1024 ** 3
    owner_sessions: int = 8
    dataset_sessions: int = 32
    owner_bytes: int = 1024 ** 4
    dataset_bytes: int = 4 * 1024 ** 4
    signing_interval_seconds: int = 5

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "MultipartStorageConfig":
        if env is None:
            from app.config import get_settings

            env = os.environ
            upload_bucket = get_settings().storage.upload_bucket
            backend = get_settings().storage.backend
        else:
            upload_bucket = env.get("UPLOAD_BUCKET", "").strip() or None
            backend = env.get("STORAGE_BACKEND", "").strip().lower() or "gcs"
        flag = env.get("MULTIPART_UPLOAD_ENABLED", "false").strip().lower()
        if flag not in {"true", "false"}:
            raise ValueError("MULTIPART_UPLOAD_ENABLED must be true or false")
        config = cls(
            enabled=flag == "true",
            max_upload_bytes=min(int(env.get("MAX_UPLOAD_BYTES", str(500 * 1024 ** 3))), 500 * 1024 ** 3),
            **{name: int(env.get("MULTIPART_" + name.upper(), str(default))) for name, default in {
                "owner_sessions": 8, "dataset_sessions": 32,
                "owner_bytes": 1024 ** 4, "dataset_bytes": 4 * 1024 ** 4,
                "signing_interval_seconds": 5,
            }.items()},
            bucket=env.get("MULTIPART_UPLOAD_BUCKET", "").strip() or None,
            signer_service_account=env.get("MULTIPART_UPLOAD_SIGNER", "").strip() or None,
            project=env.get("MULTIPART_UPLOAD_PROJECT", "").strip() or None,
        )
        if min(config.max_upload_bytes, config.owner_sessions, config.dataset_sessions, config.owner_bytes, config.dataset_bytes, config.signing_interval_seconds) < 1:
            raise ValueError("Multipart limits must be positive")
        if config.enabled and not config.bucket:
            raise ValueError("Multipart uploads require an explicit bucket")
        if config.enabled and backend == "gcs" and not (config.signer_service_account and config.project):
            raise ValueError("Multipart uploads on Google Cloud Storage require a signer and a project")
        if config.enabled and config.bucket != upload_bucket:
            raise ValueError("Multipart uploads must use UPLOAD_BUCKET, the environment's single upload bucket")
        return config

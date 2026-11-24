import os
from dataclasses import dataclass
from collections.abc import Mapping


@dataclass(frozen=True)
class MultipartStorageConfig:
    """Upload limits. Where uploads go is the store chosen for each one (STORAGE_STORES)."""
    enabled: bool = False

    max_upload_bytes: int = 500 * 1024 ** 3
    owner_sessions: int = 8
    dataset_sessions: int = 32
    owner_bytes: int = 1024 ** 4
    dataset_bytes: int = 4 * 1024 ** 4
    signing_interval_seconds: int = 5

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "MultipartStorageConfig":
        env = os.environ if env is None else env
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
        )
        if min(config.max_upload_bytes, config.owner_sessions, config.dataset_sessions, config.owner_bytes, config.dataset_bytes, config.signing_interval_seconds) < 1:
            raise ValueError("Multipart limits must be positive")
        return config

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    return max(minimum, value)


def _env_float(name: str, default: float, minimum: float = 0.01) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        return default
    return max(minimum, value)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime configuration tuned for low-memory cloud containers (e.g. Render)."""

    max_concurrent_jobs: int
    ffmpeg_threads: int
    ffmpeg_timeout_seconds: float
    max_upload_mb: int
    max_frames: int
    max_fps: float
    download_timeout_seconds: float
    allow_private_urls: bool
    cors_origins: tuple[str, ...]
    ffmpeg_path_override: str | None

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


def load_settings() -> Settings:
    raw_origins = os.getenv("CORS_ORIGINS", "*").strip()
    origins = tuple(o.strip() for o in raw_origins.split(",") if o.strip()) or ("*",)
    ffmpeg_override = os.getenv("FFMPEG_PATH", "").strip() or None

    return Settings(
        max_concurrent_jobs=_env_int("MAX_CONCURRENT_JOBS", default=2, minimum=1),
        ffmpeg_threads=_env_int("FFMPEG_THREADS", default=2, minimum=1),
        ffmpeg_timeout_seconds=_env_float("FFMPEG_TIMEOUT_SECONDS", default=60.0, minimum=5.0),
        max_upload_mb=_env_int("MAX_UPLOAD_MB", default=100, minimum=1),
        max_frames=_env_int("MAX_FRAMES", default=300, minimum=1),
        max_fps=_env_float("MAX_FPS", default=60.0, minimum=0.1),
        download_timeout_seconds=_env_float("DOWNLOAD_TIMEOUT_SECONDS", default=30.0, minimum=2.0),
        allow_private_urls=_env_bool("ALLOW_PRIVATE_URLS", default=False),
        cors_origins=origins,
        ffmpeg_path_override=ffmpeg_override,
    )


settings = load_settings()

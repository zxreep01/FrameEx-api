from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Literal

from app.config import Settings
from app.errors import APIError

FileType = Literal["single", "zip"]
ImageFormat = Literal["jpg", "png", "webp"]

IMAGE_MIME_TYPES: dict[ImageFormat, str] = {
    "jpg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
}


def _parse_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _parse_float(value: Any, field_name: str) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError) as exc:
        raise APIError(422, f"Parameter '{field_name}' must be a valid number.", "validation_error") from exc
    if not math.isfinite(num):
        raise APIError(422, f"Parameter '{field_name}' must be a finite number.", "validation_error")
    return num


def _parse_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise APIError(422, f"Parameter '{field_name}' must be an integer.", "validation_error")
    try:
        text = str(value).strip()
        num = int(text)
    except (TypeError, ValueError) as exc:
        raise APIError(422, f"Parameter '{field_name}' must be an integer.", "validation_error") from exc
    return num


@dataclass(frozen=True, slots=True)
class ExtractionOptions:
    """Validated and normalized frame extraction parameters."""

    video_url: str | None
    fps: float
    file_type: FileType
    image_format: ImageFormat
    quality: int
    frame_index: int
    timestamp: float | None
    start_time: float | None
    duration: float | None
    width: int | None
    height: int | None
    max_frames: int

    @property
    def mime_type(self) -> str:
        return IMAGE_MIME_TYPES[self.image_format]


def parse_extraction_options(raw: dict[str, Any], cfg: Settings) -> ExtractionOptions:
    video_url = _parse_optional_str(raw.get("video_url"))

    # file_type: 'single' or 'zip' (also accept output_type / delivery as convenience aliases)
    raw_file_type = (
        _parse_optional_str(raw.get("file_type"))
        or _parse_optional_str(raw.get("output_type"))
        or _parse_optional_str(raw.get("delivery"))
        or "zip"
    ).lower()
    if raw_file_type not in {"single", "zip"}:
        raise APIError(
            422,
            f"Invalid file_type '{raw_file_type}'. Supported values are 'single' (returns one frame image directly) "
            "or 'zip' (returns all extracted frames in a .zip archive). To choose image format (jpg, png, webp), "
            "use 'image_format'.",
            "validation_error",
        )
    file_type: FileType = "single" if raw_file_type == "single" else "zip"

    # fps
    raw_fps = _parse_optional_str(raw.get("fps"))
    fps = _parse_float(raw_fps, "fps") if raw_fps is not None else 1.0
    if fps <= 0 or fps > cfg.max_fps:
        raise APIError(
            422,
            f"Parameter 'fps' must be greater than 0 and at most {cfg.max_fps:g}.",
            "validation_error",
        )

    # image_format
    raw_fmt = (
        _parse_optional_str(raw.get("image_format"))
        or _parse_optional_str(raw.get("format"))
        or "jpg"
    ).lower().lstrip(".")
    if raw_fmt == "jpeg":
        raw_fmt = "jpg"
    if raw_fmt not in {"jpg", "png", "webp"}:
        raise APIError(
            422,
            f"Invalid image_format '{raw_fmt}'. Supported values are 'jpg', 'png', or 'webp'.",
            "validation_error",
        )
    image_format: ImageFormat = raw_fmt  # type: ignore[assignment]

    # quality (1..100)
    raw_quality = _parse_optional_str(raw.get("quality"))
    quality = _parse_int(raw_quality, "quality") if raw_quality is not None else 85
    if not 1 <= quality <= 100:
        raise APIError(422, "Parameter 'quality' must be between 1 and 100.", "validation_error")

    # frame_index (1-based)
    raw_frame_index = _parse_optional_str(raw.get("frame_index"))
    frame_index = _parse_int(raw_frame_index, "frame_index") if raw_frame_index is not None else 1
    if frame_index < 1:
        raise APIError(422, "Parameter 'frame_index' must be >= 1.", "validation_error")

    # timestamp (seconds, >= 0)
    raw_ts = _parse_optional_str(raw.get("timestamp"))
    timestamp = _parse_float(raw_ts, "timestamp") if raw_ts is not None else None
    if timestamp is not None and timestamp < 0:
        raise APIError(422, "Parameter 'timestamp' must be >= 0.", "validation_error")

    # start_time (seconds, >= 0)
    raw_start = _parse_optional_str(raw.get("start_time"))
    start_time = _parse_float(raw_start, "start_time") if raw_start is not None else None
    if start_time is not None and start_time < 0:
        raise APIError(422, "Parameter 'start_time' must be >= 0.", "validation_error")

    # duration (seconds, > 0)
    raw_duration = _parse_optional_str(raw.get("duration"))
    duration = _parse_float(raw_duration, "duration") if raw_duration is not None else None
    if duration is not None and duration <= 0:
        raise APIError(422, "Parameter 'duration' must be > 0.", "validation_error")

    # width / height
    raw_width = _parse_optional_str(raw.get("width"))
    width = _parse_int(raw_width, "width") if raw_width is not None else None
    if width is not None and not 16 <= width <= 4096:
        raise APIError(422, "Parameter 'width' must be between 16 and 4096 pixels.", "validation_error")

    raw_height = _parse_optional_str(raw.get("height"))
    height = _parse_int(raw_height, "height") if raw_height is not None else None
    if height is not None and not 16 <= height <= 4096:
        raise APIError(422, "Parameter 'height' must be between 16 and 4096 pixels.", "validation_error")

    # max_frames
    raw_max_frames = _parse_optional_str(raw.get("max_frames"))
    if raw_max_frames is not None:
        max_frames = _parse_int(raw_max_frames, "max_frames")
        if not 1 <= max_frames <= cfg.max_frames:
            raise APIError(
                422,
                f"Parameter 'max_frames' must be between 1 and {cfg.max_frames}.",
                "validation_error",
            )
    else:
        max_frames = cfg.max_frames

    return ExtractionOptions(
        video_url=video_url,
        fps=fps,
        file_type=file_type,
        image_format=image_format,
        quality=quality,
        frame_index=frame_index,
        timestamp=timestamp,
        start_time=start_time,
        duration=duration,
        width=width,
        height=height,
        max_frames=max_frames,
    )

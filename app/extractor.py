from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
import shutil
from typing import AsyncIterator

from app.config import Settings
from app.errors import APIError
from app.schemas import ExtractionOptions, ImageFormat


def _fmt_num(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".") or "0"


@lru_cache(maxsize=4)
def resolve_ffmpeg_binary(override: str | None = None) -> str:
    """Locate an executable FFmpeg binary (explicit env -> system PATH -> bundled imageio-ffmpeg)."""
    if override:
        path = Path(override)
        if path.is_file():
            return str(path)
        found = shutil.which(override)
        if found:
            return found
        raise RuntimeError(f"Configured FFMPEG_PATH '{override}' was not found.")

    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg

    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise RuntimeError("FFmpeg binary is not installed or accessible.") from exc


def _codec_args(image_format: ImageFormat, quality: int) -> list[str]:
    if image_format == "jpg":
        # FFmpeg mjpeg -q:v scale: 2 (best) .. 31 (smallest)
        qscale = max(2, min(31, round(31 - (quality - 1) * 29 / 99)))
        return ["-c:v", "mjpeg", "-q:v", str(qscale)]
    if image_format == "png":
        return ["-c:v", "png", "-compression_level", "4"]
    return ["-c:v", "libwebp", "-quality", str(quality)]


def _scale_filter(width: int | None, height: int | None) -> str | None:
    if width is None and height is None:
        return None
    w = width if width is not None else -1
    h = height if height is not None else -1
    return f"scale={w}:{h}"


def _classify_ffmpeg_error(stderr_text: str, sought_offset: float = 0.0) -> APIError:
    lower = stderr_text.lower()
    if "does not contain any stream" in lower or "matches no streams" in lower:
        return APIError(
            400,
            "The provided file does not contain a decodable video or GIF stream.",
            "no_video_stream",
        )
    if (
        "invalid data found when processing input" in lower
        or "moov atom not found" in lower
        or "error opening input" in lower
    ):
        return APIError(
            400,
            "Invalid, unsupported, or corrupted video/GIF file.",
            "invalid_media",
        )
    if sought_offset > 0 or "nothing was encoded" in lower or "output file is empty" in lower:
        return APIError(
            400,
            "No frames were extracted. The requested timestamp, start_time, or frame_index may exceed the video/GIF duration.",
            "empty_extraction",
        )
    return APIError(
        400,
        "Failed to extract frames from the provided media.",
        "extraction_failed",
    )


class FrameExtractor:
    """Async, concurrency-bounded FFmpeg frame extraction service."""

    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        self.ffmpeg_bin = resolve_ffmpeg_binary(cfg.ffmpeg_path_override)
        self._semaphore = asyncio.Semaphore(cfg.max_concurrent_jobs)
        self._active_jobs = 0

    @property
    def active_jobs(self) -> int:
        return self._active_jobs

    @asynccontextmanager
    async def _job_slot(self) -> AsyncIterator[None]:
        async with self._semaphore:
            self._active_jobs += 1
            try:
                yield
            finally:
                self._active_jobs -= 1

    async def _run_ffmpeg(self, cmd: list[str]) -> tuple[int, bytes, str]:
        async with self._job_slot():
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=self.cfg.ffmpeg_timeout_seconds,
                )
            except asyncio.TimeoutError as exc:
                proc.kill()
                await proc.communicate()
                raise APIError(
                    504,
                    f"Frame extraction timed out after {self.cfg.ffmpeg_timeout_seconds:g}s.",
                    "extraction_timeout",
                ) from exc

            return proc.returncode or 0, stdout, stderr.decode("utf-8", errors="replace").strip()

    async def extract_single_frame(self, input_path: Path, options: ExtractionOptions) -> bytes:
        """Decode and return a single frame directly in memory via FFmpeg image2pipe."""
        if options.timestamp is not None:
            seek_ts = options.timestamp
        else:
            seek_ts = (options.start_time or 0.0) + (options.frame_index - 1) / options.fps

        cmd: list[str] = [
            self.ffmpeg_bin,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-threads",
            str(self.cfg.ffmpeg_threads),
        ]
        if seek_ts > 0:
            cmd.extend(["-ss", _fmt_num(seek_ts)])
        cmd.extend(["-i", str(input_path)])

        scale = _scale_filter(options.width, options.height)
        if scale:
            cmd.extend(["-vf", scale])

        cmd.extend(
            [
                "-frames:v",
                "1",
                "-f",
                "image2pipe",
                *_codec_args(options.image_format, options.quality),
                "-",
            ]
        )

        rc, stdout, stderr_text = await self._run_ffmpeg(cmd)
        if rc != 0 or not stdout:
            raise _classify_ffmpeg_error(stderr_text, sought_offset=seek_ts)

        return stdout

    async def extract_zip_frames(
        self,
        input_path: Path,
        frames_dir: Path,
        options: ExtractionOptions,
    ) -> list[tuple[str, Path]]:
        """Extract multiple frames at requested FPS to frames_dir for streaming ZIP delivery."""
        frames_dir.mkdir(parents=True, exist_ok=True)
        pattern = frames_dir / f"frame_%05d.{options.image_format}"

        cmd: list[str] = [
            self.ffmpeg_bin,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-threads",
            str(self.cfg.ffmpeg_threads),
        ]
        if options.start_time is not None and options.start_time > 0:
            cmd.extend(["-ss", _fmt_num(options.start_time)])

        cmd.extend(["-i", str(input_path)])

        if options.duration is not None:
            cmd.extend(["-t", _fmt_num(options.duration)])

        filters = [f"fps={_fmt_num(options.fps)}:eof_action=pass"]
        scale = _scale_filter(options.width, options.height)
        if scale:
            filters.append(scale)

        cmd.extend(
            [
                "-vf",
                ",".join(filters),
                "-frames:v",
                str(options.max_frames),
                "-f",
                "image2",
                *_codec_args(options.image_format, options.quality),
                str(pattern),
            ]
        )

        rc, _stdout, stderr_text = await self._run_ffmpeg(cmd)
        files = sorted(
            p
            for p in frames_dir.glob(f"frame_*.{options.image_format}")
            if p.is_file() and p.stat().st_size > 0
        )

        if not files:
            raise _classify_ffmpeg_error(stderr_text, sought_offset=options.start_time or 0.0)

        return [(p.name, p) for p in files]

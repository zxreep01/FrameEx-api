from __future__ import annotations

from contextlib import asynccontextmanager
import json
from pathlib import Path
import shutil
import tempfile
from typing import Annotated, Any, AsyncIterator

from fastapi import FastAPI, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import httpx
from starlette.background import BackgroundTask
from starlette.datastructures import UploadFile as StarletteUploadFile

from app import __version__
from app.config import Settings, settings as default_settings
from app.downloader import download_video_url, save_raw_stream, save_upload_file
from app.errors import APIError, register_exception_handlers
from app.extractor import FrameExtractor
from app.schemas import ExtractionOptions, FileType, ImageFormat, parse_extraction_options
from app.zip_stream import calculate_zip_size, stream_zip_stored

API_DESCRIPTION = """
**FrameEx API** extracts frames from videos (`mp4`, `webm`, `mov`, `mkv`, `avi`, etc.) and animated/static `gif` files at your desired frame rate (`fps`) and delivers them directly in the HTTP response.

### Delivery Modes (`file_type`)
- **`zip`** — Extracts frames at `fps` (e.g. `2` or `20` FPS) and streams a `.zip` archive (`application/zip`) containing `frame_00001.<ext>`, `frame_00002.<ext>`, ...
- **`single`** — Directly delivers a single extracted frame image (`image/jpeg`, `image/png`, or `image/webp`). Supports selecting a frame via `frame_index` (1-based at `fps`) or exact `timestamp` (in seconds).

### Input Sources
- **`file`** — Upload a video or GIF directly via `multipart/form-data` (or raw binary body).
- **`video_url`** — Pass a direct `http://` or `https://` URL via `multipart/form-data`, `application/json`, or `GET /extract` query parameters.
"""

POST_EXTRACT_OPENAPI: dict[str, Any] = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "file": {
                            "type": "string",
                            "format": "binary",
                            "description": "Video or GIF file upload (provide either 'file' or 'video_url').",
                        },
                        "video_url": {
                            "type": "string",
                            "format": "uri",
                            "description": "Direct HTTP/HTTPS URL to a video or GIF (provide either 'video_url' or 'file').",
                        },
                        "fps": {
                            "type": "number",
                            "default": 1.0,
                            "example": 2,
                            "description": "Frames per second to extract (e.g. 2 or 20).",
                        },
                        "file_type": {
                            "type": "string",
                            "enum": ["zip", "single"],
                            "default": "zip",
                            "description": "Delivery file type: 'zip' for a .zip archive of frames, or 'single' for a direct single image.",
                        },
                        "image_format": {
                            "type": "string",
                            "enum": ["jpg", "png", "webp"],
                            "default": "jpg",
                            "description": "Output image format for extracted frame(s).",
                        },
                        "quality": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 100,
                            "default": 85,
                            "description": "Image compression quality (1-100) for jpg and webp.",
                        },
                        "frame_index": {
                            "type": "integer",
                            "minimum": 1,
                            "default": 1,
                            "description": "1-based frame index to extract when file_type='single' and timestamp is not set.",
                        },
                        "timestamp": {
                            "type": "number",
                            "minimum": 0,
                            "description": "Exact timestamp in seconds to extract when file_type='single'.",
                        },
                        "start_time": {
                            "type": "number",
                            "minimum": 0,
                            "description": "Optional start time offset in seconds.",
                        },
                        "duration": {
                            "type": "number",
                            "exclusiveMinimum": 0,
                            "description": "Optional clip duration in seconds to extract from.",
                        },
                        "width": {
                            "type": "integer",
                            "minimum": 16,
                            "maximum": 4096,
                            "description": "Optional output width in pixels (aspect ratio preserved if height omitted).",
                        },
                        "height": {
                            "type": "integer",
                            "minimum": 16,
                            "maximum": 4096,
                            "description": "Optional output height in pixels (aspect ratio preserved if width omitted).",
                        },
                        "max_frames": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "Optional upper bound on total extracted frames for zip output.",
                        },
                    },
                }
            },
            "application/json": {
                "schema": {
                    "type": "object",
                    "required": ["video_url"],
                    "properties": {
                        "video_url": {
                            "type": "string",
                            "format": "uri",
                            "example": "https://example.com/sample.mp4",
                        },
                        "fps": {"type": "number", "default": 1.0, "example": 2},
                        "file_type": {
                            "type": "string",
                            "enum": ["zip", "single"],
                            "default": "zip",
                            "example": "zip",
                        },
                        "image_format": {
                            "type": "string",
                            "enum": ["jpg", "png", "webp"],
                            "default": "jpg",
                        },
                        "quality": {"type": "integer", "minimum": 1, "maximum": 100, "default": 85},
                        "frame_index": {"type": "integer", "minimum": 1, "default": 1},
                        "timestamp": {"type": "number", "minimum": 0},
                        "start_time": {"type": "number", "minimum": 0},
                        "duration": {"type": "number", "exclusiveMinimum": 0},
                        "width": {"type": "integer", "minimum": 16, "maximum": 4096},
                        "height": {"type": "integer", "minimum": 16, "maximum": 4096},
                        "max_frames": {"type": "integer", "minimum": 1},
                    },
                }
            },
        },
    },
    "responses": {
        "200": {
            "description": "Directly delivers either a `.zip` archive of frames (`file_type=zip`) or a single image (`file_type=single`).",
            "headers": {
                "X-Frame-Count": {
                    "description": "Number of frames returned.",
                    "schema": {"type": "integer"},
                },
                "X-FPS": {
                    "description": "Frames-per-second rate used.",
                    "schema": {"type": "number"},
                },
                "X-Image-Format": {
                    "description": "Image format of extracted frame(s) (jpg, png, webp).",
                    "schema": {"type": "string"},
                },
            },
            "content": {
                "application/zip": {"schema": {"type": "string", "format": "binary"}},
                "image/jpeg": {"schema": {"type": "string", "format": "binary"}},
                "image/png": {"schema": {"type": "string", "format": "binary"}},
                "image/webp": {"schema": {"type": "string", "format": "binary"}},
            },
        }
    },
}


def _cleanup_dir(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


async def _execute_extraction(
    input_path: Path,
    stem: str,
    options: ExtractionOptions,
    work_dir: Path,
    extractor: FrameExtractor,
) -> Response:
    """Run FFmpeg extraction and return either a direct single-frame Response or streaming ZIP Response."""
    if options.file_type == "single":
        frame_bytes = await extractor.extract_single_frame(input_path, options)
        _cleanup_dir(work_dir)
        filename = f"{stem}_frame_{options.frame_index:05d}.{options.image_format}"
        headers = {
            "Content-Disposition": f'inline; filename="{filename}"',
            "X-Frame-Count": "1",
            "X-Frame-Index": str(options.frame_index),
            "X-FPS": f"{options.fps:g}",
            "X-Image-Format": options.image_format,
        }
        return Response(
            content=frame_bytes,
            media_type=options.mime_type,
            headers=headers,
        )

    frames_dir = work_dir / "frames"
    entries = await extractor.extract_zip_frames(input_path, frames_dir, options)

    # Free the input media file immediately before streaming the ZIP
    input_path.unlink(missing_ok=True)

    zip_size = calculate_zip_size(entries)
    zip_filename = f"{stem}_frames.zip"
    headers = {
        "Content-Disposition": f'attachment; filename="{zip_filename}"',
        "Content-Length": str(zip_size),
        "X-Frame-Count": str(len(entries)),
        "X-FPS": f"{options.fps:g}",
        "X-Image-Format": options.image_format,
    }
    return StreamingResponse(
        stream_zip_stored(entries, cleanup_dir=work_dir),
        media_type="application/zip",
        headers=headers,
        background=BackgroundTask(_cleanup_dir, work_dir),
    )


def create_app(
    cfg: Settings | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    active_cfg = cfg or default_settings

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = active_cfg
        app.state.extractor = FrameExtractor(active_cfg)
        owns_client = http_client is None
        app.state.http_client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(active_cfg.download_timeout_seconds, connect=10.0),
            follow_redirects=False,
            headers={"User-Agent": f"FrameEx-API/{__version__}"},
        )
        try:
            yield
        finally:
            if owns_client:
                await app.state.http_client.aclose()

    app = FastAPI(
        title="FrameEx API",
        version=__version__,
        description=API_DESCRIPTION,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(active_cfg.cors_origins),
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=[
            "Content-Disposition",
            "Content-Length",
            "X-Frame-Count",
            "X-Frame-Index",
            "X-FPS",
            "X-Image-Format",
        ],
    )

    register_exception_handlers(app)

    @app.get("/", tags=["System"], summary="API service metadata")
    async def root() -> dict[str, Any]:
        return {
            "name": "FrameEx API",
            "version": __version__,
            "status": "ok",
            "description": "Fast, resource-efficient API to extract frames from videos and GIFs.",
            "usage": {
                "file_types": ["zip", "single"],
                "image_formats": ["jpg", "png", "webp"],
            },
            "endpoints": {
                "extract_post": "POST /extract",
                "extract_get": "GET /extract?video_url=...&fps=2&file_type=zip",
                "health": "GET /health",
                "docs": "GET /docs",
                "redoc": "GET /redoc",
            },
        }

    @app.get("/health", tags=["System"], summary="Render health check")
    async def health(request: Request) -> dict[str, Any]:
        extractor: FrameExtractor = request.app.state.extractor
        return {
            "status": "healthy",
            "ffmpeg_available": bool(extractor.ffmpeg_bin),
            "active_jobs": extractor.active_jobs,
            "max_concurrent_jobs": active_cfg.max_concurrent_jobs,
        }

    @app.get(
        "/extract",
        tags=["Frames"],
        summary="Extract frames from a video_url via GET",
        responses=POST_EXTRACT_OPENAPI["responses"],
    )
    async def extract_get(
        request: Request,
        video_url: Annotated[
            str,
            Query(description="Direct HTTP/HTTPS URL to a video or GIF file."),
        ],
        fps: Annotated[
            float,
            Query(gt=0, description="Frames per second to extract (e.g. 2 or 20)."),
        ] = 1.0,
        file_type: Annotated[
            FileType,
            Query(
                description="Delivery file type: 'zip' returns all extracted frames in a .zip archive; 'single' returns one image directly."
            ),
        ] = "zip",
        image_format: Annotated[
            ImageFormat,
            Query(description="Output image format ('jpg', 'png', 'webp')."),
        ] = "jpg",
        quality: Annotated[
            int,
            Query(ge=1, le=100, description="Compression quality (1-100) for jpg and webp."),
        ] = 85,
        frame_index: Annotated[
            int,
            Query(ge=1, description="1-based frame index when file_type='single' and timestamp is omitted."),
        ] = 1,
        timestamp: Annotated[
            float | None,
            Query(ge=0, description="Exact timestamp in seconds when file_type='single'."),
        ] = None,
        start_time: Annotated[
            float | None,
            Query(ge=0, description="Optional start offset in seconds."),
        ] = None,
        duration: Annotated[
            float | None,
            Query(gt=0, description="Optional duration in seconds."),
        ] = None,
        width: Annotated[
            int | None,
            Query(ge=16, le=4096, description="Optional output frame width in pixels."),
        ] = None,
        height: Annotated[
            int | None,
            Query(ge=16, le=4096, description="Optional output frame height in pixels."),
        ] = None,
        max_frames: Annotated[
            int | None,
            Query(ge=1, description="Optional cap on extracted frames in zip mode."),
        ] = None,
    ) -> Response:
        raw_params: dict[str, Any] = {
            "video_url": video_url,
            "fps": fps,
            "file_type": file_type,
            "image_format": image_format,
            "quality": quality,
            "frame_index": frame_index,
            "timestamp": timestamp,
            "start_time": start_time,
            "duration": duration,
            "width": width,
            "height": height,
            "max_frames": max_frames,
        }
        options = parse_extraction_options(raw_params, active_cfg)
        if not options.video_url:
            raise APIError(400, "Parameter 'video_url' is required.", "missing_input")

        work_dir = Path(tempfile.mkdtemp(prefix="frameex_"))
        try:
            input_path, stem = await download_video_url(
                options.video_url,
                work_dir,
                active_cfg,
                http_client=request.app.state.http_client,
            )
            return await _execute_extraction(
                input_path=input_path,
                stem=stem,
                options=options,
                work_dir=work_dir,
                extractor=request.app.state.extractor,
            )
        except Exception:
            _cleanup_dir(work_dir)
            raise

    @app.post(
        "/extract",
        tags=["Frames"],
        summary="Extract frames from an uploaded video/GIF or video_url",
        openapi_extra=POST_EXTRACT_OPENAPI,
    )
    async def extract_post(request: Request) -> Response:
        raw_params: dict[str, Any] = dict(request.query_params)
        content_type = (request.headers.get("content-type") or "").lower()

        work_dir = Path(tempfile.mkdtemp(prefix="frameex_"))
        try:
            input_path: Path | None = None
            stem = "media"

            if "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
                async with request.form() as form:
                    upload_candidate: StarletteUploadFile | None = None
                    for key, value in form.multi_items():
                        if isinstance(value, StarletteUploadFile):
                            if key in {"file", "video"} and value.filename:
                                upload_candidate = value
                        else:
                            if str(value).strip() != "":
                                raw_params[key] = value

                    options = parse_extraction_options(raw_params, active_cfg)

                    if upload_candidate is not None and options.video_url:
                        raise APIError(
                            400,
                            "Provide either an uploaded 'file' or 'video_url', not both.",
                            "ambiguous_input",
                        )

                    if upload_candidate is not None:
                        input_path, stem = await save_upload_file(upload_candidate, work_dir, active_cfg)

            elif "application/json" in content_type:
                body_bytes = await request.body()
                if body_bytes:
                    try:
                        payload = json.loads(body_bytes)
                    except json.JSONDecodeError as exc:
                        raise APIError(400, "Invalid JSON payload.", "invalid_json") from exc
                    if not isinstance(payload, dict):
                        raise APIError(400, "JSON body must be an object.", "invalid_json")
                    for key, value in payload.items():
                        if value is not None:
                            raw_params[key] = value

                options = parse_extraction_options(raw_params, active_cfg)

            elif content_type.startswith(("video/", "image/", "application/octet-stream")):
                options = parse_extraction_options(raw_params, active_cfg)
                if not options.video_url:
                    input_path, stem = await save_raw_stream(
                        request.stream(),
                        content_type,
                        work_dir,
                        active_cfg,
                    )
            else:
                options = parse_extraction_options(raw_params, active_cfg)

            if input_path is None:
                if not options.video_url:
                    raise APIError(
                        400,
                        "No video or GIF supplied. Provide either a file upload ('file') or 'video_url'.",
                        "missing_input",
                    )
                input_path, stem = await download_video_url(
                    options.video_url,
                    work_dir,
                    active_cfg,
                    http_client=request.app.state.http_client,
                )

            return await _execute_extraction(
                input_path=input_path,
                stem=stem,
                options=options,
                work_dir=work_dir,
                extractor=request.app.state.extractor,
            )
        except Exception:
            _cleanup_dir(work_dir)
            raise

    # Convenience /api/extract aliases for clients expecting an /api prefix
    app.add_api_route("/api/extract", extract_get, methods=["GET"], include_in_schema=False)
    app.add_api_route("/api/extract", extract_post, methods=["POST"], include_in_schema=False)

    return app


app = create_app()

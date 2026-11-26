from __future__ import annotations

import io
from pathlib import Path
import struct
import tempfile
import zipfile

from fastapi.testclient import TestClient
import httpx
import pytest

from app import downloader
from app.config import Settings
from app.main import create_app


def _make_settings(**overrides: object) -> Settings:
    defaults = dict(
        max_concurrent_jobs=2,
        ffmpeg_threads=2,
        ffmpeg_timeout_seconds=30.0,
        max_upload_mb=10,
        max_frames=200,
        max_fps=60.0,
        download_timeout_seconds=10.0,
        allow_private_urls=False,
        cors_origins=("*",),
        ffmpeg_path_override=None,
    )
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


@pytest.fixture()
def client(sample_media: dict[str, bytes], monkeypatch: pytest.MonkeyPatch):
    """TestClient wired with a deterministic mock remote CDN for video_url tests."""

    # Allow synthetic 'cdn.example.com' DNS resolution without hitting external DNS in tests,
    # while keeping SSRF checks active for private/loopback literals and redirects.
    real_validate = downloader.validate_public_url

    async def fake_validate(url: str, allow_private_urls: bool = False):
        if "cdn.example.com" in url:
            return url, httpx.URL(url).path
        return await real_validate(url, allow_private_urls=allow_private_urls)

    monkeypatch.setattr(downloader, "validate_public_url", fake_validate)

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/sample.mp4":
            return httpx.Response(
                200,
                headers={"Content-Type": "video/mp4", "Content-Length": str(len(sample_media["mp4"]))},
                content=sample_media["mp4"],
            )
        if path == "/anim.gif":
            return httpx.Response(
                200,
                headers={"Content-Type": "image/gif"},
                content=sample_media["gif"],
            )
        if path == "/redirect-mp4":
            return httpx.Response(302, headers={"Location": "https://cdn.example.com/sample.mp4"})
        if path == "/redirect-ssrf":
            return httpx.Response(302, headers={"Location": "http://127.0.0.1:8080/internal"})
        if path == "/page.html":
            return httpx.Response(200, headers={"Content-Type": "text/html; charset=utf-8"}, text="<html>nope</html>")
        if path == "/huge.mp4":
            return httpx.Response(
                200,
                headers={"Content-Type": "video/mp4", "Content-Length": str(50 * 1024 * 1024)},
                content=b"0000",
            )
        return httpx.Response(404, text="Not found")

    mock_http = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    app = create_app(cfg=_make_settings(), http_client=mock_http)
    with TestClient(app) as test_client:
        yield test_client


def test_root_and_health_and_docs(client: TestClient) -> None:
    r_root = client.get("/")
    assert r_root.status_code == 200
    assert r_root.json()["status"] == "ok"

    r_health = client.get("/health")
    assert r_health.status_code == 200
    body = r_health.json()
    assert body["status"] == "healthy"
    assert body["ffmpeg_available"] is True

    assert client.get("/docs").status_code == 200
    openapi = client.get("/openapi.json")
    assert openapi.status_code == 200
    assert "/extract" in openapi.json()["paths"]


def test_upload_mp4_zip_fps_2_and_20(client: TestClient, sample_media: dict[str, bytes]) -> None:
    # 2.0s video at fps=2 -> 4 frames
    res_2 = client.post(
        "/extract",
        files={"file": ("clip.mp4", sample_media["mp4"], "video/mp4")},
        data={"fps": "2", "file_type": "zip"},
    )
    assert res_2.status_code == 200
    assert res_2.headers["content-type"] == "application/zip"
    assert res_2.headers["x-frame-count"] == "4"
    assert int(res_2.headers["content-length"]) == len(res_2.content)
    assert 'filename="clip_frames.zip"' in res_2.headers["content-disposition"]

    with zipfile.ZipFile(io.BytesIO(res_2.content)) as zf:
        assert zf.testzip() is None
        names = sorted(zf.namelist())
        assert names == ["frame_00001.jpg", "frame_00002.jpg", "frame_00003.jpg", "frame_00004.jpg"]
        for name in names:
            assert zf.read(name).startswith(b"\xff\xd8\xff")

    # 2.0s video at fps=20 -> 40 frames
    res_20 = client.post(
        "/extract",
        files={"file": ("clip.mp4", sample_media["mp4"], "video/mp4")},
        data={"fps": "20", "file_type": "zip"},
    )
    assert res_20.status_code == 200
    assert res_20.headers["x-frame-count"] == "40"
    with zipfile.ZipFile(io.BytesIO(res_20.content)) as zf:
        assert zf.testzip() is None
        assert len(zf.namelist()) == 40


def test_upload_single_frame_formats_and_selection(client: TestClient, sample_media: dict[str, bytes]) -> None:
    # Default jpg single frame
    r_jpg = client.post(
        "/extract",
        files={"file": ("clip.mp4", sample_media["mp4"], "video/mp4")},
        data={"fps": "2", "file_type": "single"},
    )
    assert r_jpg.status_code == 200
    assert r_jpg.headers["content-type"] == "image/jpeg"
    assert r_jpg.headers["x-frame-count"] == "1"
    assert r_jpg.content.startswith(b"\xff\xd8\xff")

    # PNG single frame at frame_index=3 with resize width=80
    r_png = client.post(
        "/extract",
        files={"file": ("clip.mp4", sample_media["mp4"], "video/mp4")},
        data={"fps": "2", "file_type": "single", "frame_index": "3", "image_format": "png", "width": "80"},
    )
    assert r_png.status_code == 200
    assert r_png.headers["content-type"] == "image/png"
    assert r_png.content.startswith(b"\x89PNG\r\n\x1a\n")
    # Verify PNG IHDR width is exactly 80 px
    png_width, png_height = struct.unpack(">II", r_png.content[16:24])
    assert png_width == 80
    assert png_height == 60

    # WebP single frame at explicit timestamp=1.25
    r_webp = client.post(
        "/extract",
        files={"file": ("clip.mp4", sample_media["mp4"], "video/mp4")},
        data={"file_type": "single", "timestamp": "1.25", "image_format": "webp"},
    )
    assert r_webp.status_code == 200
    assert r_webp.headers["content-type"] == "image/webp"
    assert r_webp.content[:4] == b"RIFF" and r_webp.content[8:12] == b"WEBP"


def test_animated_and_static_gif_extraction(client: TestClient, sample_media: dict[str, bytes]) -> None:
    # Animated 1.5s GIF -> WebP frames in ZIP (verifies individual WebP frames, not animated webp)
    r_gif_zip = client.post(
        "/extract",
        files={"file": ("party.gif", sample_media["gif"], "image/gif")},
        data={"fps": "4", "file_type": "zip", "image_format": "webp"},
    )
    assert r_gif_zip.status_code == 200
    assert r_gif_zip.headers["x-frame-count"] == "6"
    with zipfile.ZipFile(io.BytesIO(r_gif_zip.content)) as zf:
        assert zf.testzip() is None
        assert len(zf.namelist()) == 6
        for name in zf.namelist():
            raw = zf.read(name)
            assert raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"

    # 1-frame static GIF with fps=2 -> cleanly extracts 1 frame without empty-filter error
    r_static = client.post(
        "/extract",
        files={"file": ("logo.gif", sample_media["static_gif"], "image/gif")},
        data={"fps": "2", "file_type": "zip", "image_format": "png"},
    )
    assert r_static.status_code == 200
    assert r_static.headers["x-frame-count"] == "1"


def test_video_url_json_post_get_and_redirects(client: TestClient) -> None:
    # JSON POST with video_url + redirect
    r_json = client.post(
        "/extract",
        json={"video_url": "https://cdn.example.com/redirect-mp4", "fps": 2, "file_type": "zip"},
    )
    assert r_json.status_code == 200
    assert r_json.headers["content-type"] == "application/zip"
    assert r_json.headers["x-frame-count"] == "4"

    # GET /extract with video_url returning single PNG frame
    r_get = client.get(
        "/extract",
        params={
            "video_url": "https://cdn.example.com/anim.gif",
            "fps": 2,
            "file_type": "single",
            "frame_index": 2,
            "image_format": "png",
        },
    )
    assert r_get.status_code == 200
    assert r_get.headers["content-type"] == "image/png"
    assert r_get.content.startswith(b"\x89PNG\r\n\x1a\n")


def test_raw_binary_post_and_slice_controls(client: TestClient, sample_media: dict[str, bytes]) -> None:
    r_raw = client.post(
        "/extract?fps=4&file_type=zip&start_time=0.5&duration=1.0&max_frames=3",
        content=sample_media["mp4"],
        headers={"Content-Type": "video/mp4"},
    )
    assert r_raw.status_code == 200
    assert r_raw.headers["x-frame-count"] == "3"


def test_validation_security_and_cleanup(client: TestClient, sample_media: dict[str, bytes]) -> None:
    before_dirs = set(Path(tempfile.gettempdir()).glob("frameex_*"))

    # Invalid file_type
    r_bad_ft = client.post(
        "/extract",
        json={"video_url": "https://cdn.example.com/sample.mp4", "fps": 2, "file_type": "jpg"},
    )
    assert r_bad_ft.status_code == 422
    assert "single" in r_bad_ft.json()["error"]["message"]

    # Missing input
    r_missing = client.post("/extract", data={"fps": "2", "file_type": "zip"})
    assert r_missing.status_code == 400

    # Both file and video_url provided
    r_both = client.post(
        "/extract",
        files={"file": ("clip.mp4", sample_media["mp4"], "video/mp4")},
        data={"video_url": "https://cdn.example.com/sample.mp4", "fps": "2", "file_type": "zip"},
    )
    assert r_both.status_code == 400

    # Corrupted media
    r_corrupt = client.post(
        "/extract",
        files={"file": ("fake.mp4", b"not a real video", "video/mp4")},
        data={"fps": "2", "file_type": "single"},
    )
    assert r_corrupt.status_code == 400
    assert r_corrupt.json()["error"]["code"] == "invalid_media"

    # Audio-only file
    r_audio = client.post(
        "/extract",
        files={"file": ("tone.wav", sample_media["wav"], "audio/wav")},
        data={"fps": "2", "file_type": "single"},
    )
    assert r_audio.status_code == 400
    assert r_audio.json()["error"]["code"] == "no_video_stream"

    # Timestamp beyond video duration
    r_oob = client.post(
        "/extract",
        files={"file": ("clip.mp4", sample_media["mp4"], "video/mp4")},
        data={"file_type": "single", "timestamp": "30"},
    )
    assert r_oob.status_code == 400
    assert r_oob.json()["error"]["code"] == "empty_extraction"

    # HTML URL rejected
    r_html = client.post(
        "/extract",
        json={"video_url": "https://cdn.example.com/page.html", "file_type": "single"},
    )
    assert r_html.status_code == 400
    assert r_html.json()["error"]["code"] == "invalid_media_url"

    # Oversized remote file rejected (413)
    r_huge = client.post(
        "/extract",
        json={"video_url": "https://cdn.example.com/huge.mp4", "file_type": "zip"},
    )
    assert r_huge.status_code == 413

    # SSRF protection: loopback, cloud metadata IP, non-HTTP scheme, and redirect-to-localhost
    for blocked_url in [
        "http://127.0.0.1:8000/health",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/video.mp4",
        "file:///etc/passwd",
        "https://cdn.example.com/redirect-ssrf",
    ]:
        r_ssrf = client.post("/extract", json={"video_url": blocked_url, "file_type": "single"})
        assert r_ssrf.status_code == 400, f"Expected 400 for {blocked_url}, got {r_ssrf.status_code}"

    # Verify zero leftover temporary directories
    after_dirs = set(Path(tempfile.gettempdir()).glob("frameex_*"))
    assert after_dirs - before_dirs == set()


def test_presigned_query_url_video_alias_and_upload_limit(
    client: TestClient,
    sample_media: dict[str, bytes],
) -> None:
    # Presigned-style URL with query params + uppercase FILE_TYPE
    r_presigned = client.post(
        "/extract",
        json={
            "video_url": "https://cdn.example.com/sample.mp4?token=xyz123&expires=1799999999",
            "fps": 0.5,
            "file_type": "ZIP",
        },
    )
    assert r_presigned.status_code == 200
    assert 'filename="sample_frames.zip"' in r_presigned.headers["content-disposition"]
    assert r_presigned.headers["x-frame-count"] == "1"

    # 'video' form field alias + /api/extract route alias
    r_alias = client.post(
        "/api/extract",
        files={"video": ("demo.gif", sample_media["gif"], "image/gif")},
        data={"fps": "2", "file_type": "SINGLE"},
    )
    assert r_alias.status_code == 200
    assert r_alias.headers["content-type"] == "image/jpeg"

    # Direct multipart upload size limit enforcement (1 MB max)
    tiny_app = create_app(cfg=_make_settings(max_upload_mb=1))
    with TestClient(tiny_app) as tiny_client:
        oversized_payload = b"0" * (1024 * 1024 + 2048)
        r_too_big = tiny_client.post(
            "/extract",
            files={"file": ("big.mp4", oversized_payload, "video/mp4")},
            data={"fps": "2", "file_type": "single"},
        )
        assert r_too_big.status_code == 413


# FrameEx API

A fast, low-memory REST API built with **FastAPI**, **Uvicorn (`uvloop` + `httptools`)**, and native **FFmpeg** to extract frames from videos (`mp4`, `webm`, `mov`, `mkv`, `avi`, etc.) and `gif` animations at any target FPS (e.g. `2` or `20`) and deliver them directly to the caller.

Designed specifically to run efficiently on **Render** (including the 512 MB Free & Starter tiers):
- **Zero-buffering input streaming**: Uploads and `video_url` downloads stream in 64 KiB chunks directly to ephemeral storage with strict size limits.
- **Zero-recompression streaming ZIP (`ZIP_STORED`)**: Frames are already compressed (`jpg`, `png`, `webp`), so `file_type=zip` streams a spec-compliant PKZIP archive one frame at a time with exact `Content-Length`, near-zero CPU overhead, `< 1 MB` RAM overhead, and immediate per-frame disk cleanup.
- **Direct pipe for single frame (`file_type=single`)**: Uses fast keyframe seeking + `image2pipe` to decode only 1 frame and return it directly as `image/jpeg`, `image/png`, or `image/webp`.
- **Bounded concurrency**: Semaphore-controlled FFmpeg worker slots prevent burst traffic from exhausting container RAM/CPU.
- **SSRF protection**: Blocks loopback, private RFC1918, and cloud metadata addresses on `video_url` (including redirect hops).

---

## Endpoints

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/extract` | Extract frames from an uploaded `file` (`multipart/form-data` or raw binary) or a `video_url` (`multipart/form-data` or `application/json`). |
| `GET` | `/extract` | Extract frames from a `video_url` using query parameters. |
| `GET` | `/health` | Health check endpoint for Render (`200 OK`). |
| `GET` | `/docs` | Interactive OpenAPI / Swagger UI documentation. |
| `GET` | `/redoc` | ReDoc API reference documentation. |

---

## Request Parameters

Parameters can be passed as `multipart/form-data` fields, `application/json` properties, or URL query parameters:

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `file` *(or `video`)* | binary file | — | Direct video or GIF upload (`POST /extract`). Provide either `file` or `video_url`. |
| `video_url` | `string` | — | Direct `http://` or `https://` URL to a video or GIF. Provide either `video_url` or `file`. |
| `fps` | `number` | `1.0` | Frames per second to extract (e.g. `2`, `20`, `0.5`). |
| `file_type` | `"zip"` \| `"single"` | `"zip"` | **Delivery file type**: `"zip"` streams all extracted frames in a `.zip` archive; `"single"` returns a single frame image directly. |
| `image_format` | `"jpg"` \| `"png"` \| `"webp"` | `"jpg"` | Output frame image format (`jpeg` is also accepted). |
| `quality` | `integer` (`1`–`100`) | `85` | Compression quality for `jpg` and `webp`. |
| `frame_index` | `integer` (`>= 1`) | `1` | When `file_type="single"` and `timestamp` is omitted, selects the 1-based frame index sampled at `fps`. |
| `timestamp` | `number` (`>= 0`) | — | When `file_type="single"`, extracts the frame at the exact timestamp in seconds (e.g. `1.5`). |
| `start_time` | `number` (`>= 0`) | — | Optional start time in seconds to begin extracting from. |
| `duration` | `number` (`> 0`) | — | Optional clip duration in seconds to extract over. |
| `width` | `integer` (`16`–`4096`) | — | Optional output width in pixels (aspect ratio preserved if `height` is omitted). |
| `height` | `integer` (`16`–`4096`) | — | Optional output height in pixels (aspect ratio preserved if `width` is omitted). |
| `max_frames` | `integer` (`>= 1`) | `300` | Optional cap on total frames extracted in `"zip"` mode (up to server `MAX_FRAMES`). |

### Response Headers
Every `/extract` response includes metadata headers:
- `X-Frame-Count`: Total number of frames delivered (`1` for `single`, `N` for `zip`)
- `X-FPS`: FPS value used
- `X-Image-Format`: `jpg`, `png`, or `webp`
- `X-Frame-Index`: Returned frame index (when `file_type=single`)

---

## Usage Examples

### 1. Upload a Video or GIF directly (`file_type=zip`, `fps=2` or `fps=20`)
```bash
# Extract 2 frames per second as a ZIP archive
curl -X POST "https://your-service.onrender.com/extract" \
  -F "file=@sample.mp4" \
  -F "fps=2" \
  -F "file_type=zip" \
  -o frames_2fps.zip

# Extract 20 frames per second from a GIF as WebP images inside a ZIP
curl -X POST "https://your-service.onrender.com/extract" \
  -F "file=@animation.gif" \
  -F "fps=20" \
  -F "file_type=zip" \
  -F "image_format=webp" \
  -o gif_frames.zip
```

### 2. Return a Single Frame Image Directly (`file_type=single`)
```bash
# Get the 3rd frame (at 2 FPS) directly as a PNG image
curl -X POST "https://your-service.onrender.com/extract" \
  -F "file=@sample.mp4" \
  -F "fps=2" \
  -F "file_type=single" \
  -F "frame_index=3" \
  -F "image_format=png" \
  -o frame.png
```

### 3. Extract via `video_url` (JSON `POST` or `GET`)
```bash
# POST JSON with video_url -> saves frames.zip
curl -X POST "https://your-service.onrender.com/extract" \
  -H "Content-Type: application/json" \
  -d '{
    "video_url": "https://example.com/clip.mp4",
    "fps": 2,
    "file_type": "zip",
    "image_format": "jpg"
  }' \
  -o frames.zip

# GET request returning a single frame directly
curl -L "https://your-service.onrender.com/extract?video_url=https://example.com/anim.gif&fps=2&file_type=single" \
  -o single_frame.jpg
```

### 4. Python Example (`requests`)
```python
import requests

resp = requests.post(
    "https://your-service.onrender.com/extract",
    json={
        "video_url": "https://example.com/video.mp4",
        "fps": 20,
        "file_type": "zip",
        "image_format": "jpg",
    },
)
resp.raise_for_status()
with open("frames.zip", "wb") as f:
    f.write(resp.content)
print("Extracted frames:", resp.headers.get("X-Frame-Count"))
```

---

## Deploying to Render

### Option A: Render Blueprint (`render.yaml` — Recommended)
1. Push this repository to GitHub.
2. In the [Render Dashboard](https://dashboard.render.com), click **New +** -> **Blueprint**.
3. Connect your `FrameEx-api` repository — Render automatically reads `render.yaml` and configures build, start command, `/health` check, and resource limits. *(Bundled static FFmpeg via `imageio-ffmpeg` works out-of-the-box on Render's Native Python runtime without needing `apt-get`.)*

### Option B: Manual Render Web Service (Native Python)
- **Runtime**: `Python 3`
- **Build Command**: `pip install --upgrade pip && pip install -r requirements.txt`
- **Start Command**: `uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1`
- **Health Check Path**: `/health`

### Option C: Docker on Render
A slim `Dockerfile` (with system `ffmpeg` pre-installed and a non-root user) is also included if you choose **Docker** as the Render runtime.

---

## Environment Variables

| Variable | Default | Description |
| --- | --- | --- |
| `PORT` | `8000` | HTTP port (automatically injected by Render). |
| `MAX_CONCURRENT_JOBS` | `2` | Maximum simultaneous FFmpeg extraction processes. |
| `FFMPEG_THREADS` | `2` | CPU threads per FFmpeg process. |
| `FFMPEG_TIMEOUT_SECONDS` | `60` | Per-extraction timeout in seconds. |
| `MAX_UPLOAD_MB` | `100` | Maximum allowed upload or remote `video_url` file size in MB. |
| `MAX_FRAMES` | `300` | Server-wide maximum frames allowed per ZIP request. |
| `MAX_FPS` | `60` | Maximum allowed `fps` parameter value. |
| `DOWNLOAD_TIMEOUT_SECONDS` | `30` | Timeout in seconds when downloading `video_url`. |
| `ALLOW_PRIVATE_URLS` | `false` | Keep `false` in production to prevent SSRF to internal networks. |
| `CORS_ORIGINS` | `*` | Comma-separated allowed CORS origins. |

---

## Local Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

# Start server
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# Run test suite
pytest -v
```

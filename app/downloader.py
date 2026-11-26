from __future__ import annotations

import asyncio
import ipaddress
from pathlib import Path
import re
import socket
from typing import AsyncIterator
from urllib.parse import urljoin, urlparse, unquote

from fastapi import UploadFile
import httpx

from app.config import Settings
from app.errors import APIError

CHUNK_SIZE = 65536  # 64 KiB streaming buffer
MAX_REDIRECTS = 5
SAFE_STEM_RE = re.compile(r"[^A-Za-z0-9_-]+")
KNOWN_EXTENSIONS = {
    ".mp4",
    ".gif",
    ".webm",
    ".mov",
    ".mkv",
    ".avi",
    ".m4v",
    ".mpeg",
    ".mpg",
    ".ogv",
    ".3gp",
    ".ts",
    ".flv",
    ".webp",
}


def sanitize_stem(filename: str | None, fallback: str = "media") -> str:
    if not filename:
        return fallback
    base = Path(unquote(filename)).name
    stem = Path(base).stem
    cleaned = SAFE_STEM_RE.sub("_", stem).strip("_")
    return cleaned[:64] if cleaned else fallback


def infer_suffix(filename: str | None, content_type: str | None = None) -> str:
    if filename:
        ext = Path(unquote(filename)).suffix.lower()
        if ext in KNOWN_EXTENSIONS:
            return ext
    if content_type:
        ct = content_type.split(";", 1)[0].strip().lower()
        mapping = {
            "image/gif": ".gif",
            "image/webp": ".webp",
            "video/mp4": ".mp4",
            "video/webm": ".webm",
            "video/quicktime": ".mov",
            "video/x-matroska": ".mkv",
            "video/x-msvideo": ".avi",
            "video/mpeg": ".mpeg",
        }
        if ct in mapping:
            return mapping[ct]
    return ".bin"


def _is_forbidden_ip(addr: str) -> bool:
    ip = ipaddress.ip_address(addr)
    targets = [ip]
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        targets.append(ip.ipv4_mapped)
    for candidate in targets:
        if (
            candidate.is_private
            or candidate.is_loopback
            or candidate.is_link_local
            or candidate.is_multicast
            or candidate.is_reserved
            or candidate.is_unspecified
        ):
            return True
    return False


async def validate_public_url(url: str, allow_private_urls: bool = False) -> tuple[str, str]:
    """Validate URL scheme/host and block SSRF attempts against private/internal IPs."""
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise APIError(
            400,
            "Invalid video_url scheme. Only 'http://' and 'https://' URLs are supported.",
            "invalid_url",
        )
    hostname = parsed.hostname
    if not hostname:
        raise APIError(400, "Invalid video_url: missing hostname.", "invalid_url")

    if not allow_private_urls:
        # Check literal IP first before DNS lookup
        try:
            ipaddress.ip_address(hostname)
            is_literal_ip = True
        except ValueError:
            is_literal_ip = False

        if is_literal_ip:
            if _is_forbidden_ip(hostname):
                raise APIError(
                    400,
                    "Access to private, loopback, or internal network addresses is not permitted.",
                    "forbidden_url",
                )
        else:
            port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
            loop = asyncio.get_running_loop()
            try:
                infos = await loop.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
            except socket.gaierror as exc:
                raise APIError(
                    400,
                    f"Could not resolve hostname '{hostname}' from video_url.",
                    "dns_error",
                ) from exc

            if not infos:
                raise APIError(400, f"Could not resolve hostname '{hostname}'.", "dns_error")

            for info in infos:
                resolved_ip = info[4][0]
                if _is_forbidden_ip(resolved_ip):
                    raise APIError(
                        400,
                        "Access to private, loopback, or internal network addresses is not permitted.",
                        "forbidden_url",
                    )

    return url, parsed.path


async def save_upload_file(upload: UploadFile, work_dir: Path, cfg: Settings) -> tuple[Path, str]:
    """Stream multipart UploadFile directly to disk in 64 KiB chunks."""
    suffix = infer_suffix(upload.filename, upload.content_type)
    stem = sanitize_stem(upload.filename, fallback="upload")
    dest = work_dir / f"input{suffix}"

    max_bytes = cfg.max_upload_bytes
    total = 0

    with dest.open("wb") as out:
        while True:
            chunk = await upload.read(CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise APIError(
                    413,
                    f"Uploaded file exceeds maximum allowed size of {cfg.max_upload_mb} MB.",
                    "payload_too_large",
                )
            out.write(chunk)

    if total == 0:
        raise APIError(400, "Uploaded video/GIF file is empty.", "empty_file")

    return dest, stem


async def save_raw_stream(
    chunks: AsyncIterator[bytes],
    content_type: str | None,
    work_dir: Path,
    cfg: Settings,
) -> tuple[Path, str]:
    """Stream raw binary HTTP body directly to disk."""
    suffix = infer_suffix(None, content_type)
    dest = work_dir / f"input{suffix}"
    max_bytes = cfg.max_upload_bytes
    total = 0

    with dest.open("wb") as out:
        async for chunk in chunks:
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise APIError(
                    413,
                    f"Request body exceeds maximum allowed size of {cfg.max_upload_mb} MB.",
                    "payload_too_large",
                )
            out.write(chunk)

    if total == 0:
        raise APIError(
            400,
            "No video/GIF provided. Supply either a file upload ('file') or 'video_url'.",
            "missing_input",
        )

    return dest, "media"


async def download_video_url(
    video_url: str,
    work_dir: Path,
    cfg: Settings,
    http_client: httpx.AsyncClient | None = None,
) -> tuple[Path, str]:
    """Download remote video/GIF with SSRF validation on initial URL and every redirect hop."""
    current_url = video_url.strip()
    max_bytes = cfg.max_upload_bytes

    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(
        timeout=httpx.Timeout(cfg.download_timeout_seconds, connect=10.0),
        follow_redirects=False,
        headers={"User-Agent": "FrameEx-API/1.0"},
    )

    try:
        for _hop in range(MAX_REDIRECTS + 1):
            validated_url, url_path = await validate_public_url(
                current_url, allow_private_urls=cfg.allow_private_urls
            )
            try:
                async with client.stream("GET", validated_url) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise APIError(400, "Remote server sent a redirect without Location header.", "download_error")
                        current_url = urljoin(validated_url, location)
                        continue

                    if response.status_code >= 400:
                        raise APIError(
                            400,
                            f"Failed to download video_url (HTTP {response.status_code}).",
                            "download_error",
                        )

                    content_length = response.headers.get("content-length")
                    if content_length and content_length.isdigit() and int(content_length) > max_bytes:
                        raise APIError(
                            413,
                            f"Remote media exceeds maximum allowed size of {cfg.max_upload_mb} MB.",
                            "payload_too_large",
                        )

                    content_type = response.headers.get("content-type", "")
                    if content_type.lower().startswith("text/html"):
                        raise APIError(
                            400,
                            "The provided video_url returned an HTML page instead of a direct video or GIF file.",
                            "invalid_media_url",
                        )

                    suffix = infer_suffix(url_path, content_type)
                    stem = sanitize_stem(url_path, fallback="remote")
                    dest = work_dir / f"input{suffix}"

                    total = 0
                    with dest.open("wb") as out:
                        async for chunk in response.aiter_bytes(CHUNK_SIZE):
                            if not chunk:
                                continue
                            total += len(chunk)
                            if total > max_bytes:
                                raise APIError(
                                    413,
                                    f"Remote media exceeds maximum allowed size of {cfg.max_upload_mb} MB.",
                                    "payload_too_large",
                                )
                            out.write(chunk)

                    if total == 0:
                        raise APIError(400, "The downloaded file from video_url is empty.", "empty_file")

                    return dest, stem
            except httpx.TimeoutException as exc:
                raise APIError(
                    504,
                    f"Timed out while downloading media from video_url (limit: {cfg.download_timeout_seconds:g}s).",
                    "download_timeout",
                ) from exc
            except httpx.RequestError as exc:
                raise APIError(
                    400,
                    f"Network error while fetching video_url: {exc}",
                    "download_error",
                ) from exc

        raise APIError(400, f"Too many redirects (>{MAX_REDIRECTS}) while fetching video_url.", "download_error")
    finally:
        if owns_client:
            await client.aclose()

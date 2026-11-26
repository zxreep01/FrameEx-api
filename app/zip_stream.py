from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
import shutil
import struct
import time
import zlib

# Standard PKZIP format structs (ZIP 2.0, ZIP_STORED = compression method 0)
# Using ZIP_STORED avoids burning CPU re-compressing already-compressed JPEG/PNG/WebP images
# and allows computing exact Content-Length ahead of streaming with <1 MB peak RAM.
_LOCAL_HEADER_STRUCT = struct.Struct("<4sHHHHHIIIHH")
_CENTRAL_HEADER_STRUCT = struct.Struct("<4sHHHHHHIIIHHHHHII")
_EOCD_STRUCT = struct.Struct("<4sHHHHIIH")

_LOCAL_SIG = b"PK\x03\x04"
_CENTRAL_SIG = b"PK\x01\x02"
_EOCD_SIG = b"PK\x05\x06"

_ZIP_VERSION = 20
_VERSION_MADE_BY_UNIX = (3 << 8) | _ZIP_VERSION
_EXTERNAL_ATTR_REGULAR_FILE = 0o100644 << 16


def _dos_datetime(ts: float | None = None) -> tuple[int, int]:
    lt = time.localtime(ts)
    year = max(1980, min(2107, lt.tm_year))
    dos_date = ((year - 1980) << 9) | (lt.tm_mon << 5) | lt.tm_mday
    dos_time = (lt.tm_hour << 11) | (lt.tm_min << 5) | (lt.tm_sec // 2)
    return dos_time, dos_date


def calculate_zip_size(entries: list[tuple[str, Path]]) -> int:
    """Compute exact byte length of the resulting ZIP_STORED archive before streaming."""
    total = _EOCD_STRUCT.size
    for arcname, path in entries:
        name_len = len(arcname.encode("utf-8"))
        file_size = path.stat().st_size
        total += _LOCAL_HEADER_STRUCT.size + name_len + file_size
        total += _CENTRAL_HEADER_STRUCT.size + name_len
    return total


def stream_zip_stored(
    entries: list[tuple[str, Path]],
    cleanup_dir: Path | None = None,
) -> Iterator[bytes]:
    """Stream a valid PKZIP archive one frame at a time, unlinking frames immediately."""
    try:
        dos_time, dos_date = _dos_datetime()
        central_records: list[bytes] = []
        offset = 0

        for arcname, path in entries:
            name_bytes = arcname.encode("utf-8")
            data = path.read_bytes()
            # Free disk space immediately as each frame is sent
            path.unlink(missing_ok=True)

            size = len(data)
            crc = zlib.crc32(data) & 0xFFFFFFFF

            local_header = _LOCAL_HEADER_STRUCT.pack(
                _LOCAL_SIG,
                _ZIP_VERSION,
                0,          # flags
                0,          # method = ZIP_STORED
                dos_time,
                dos_date,
                crc,
                size,
                size,
                len(name_bytes),
                0,          # extra length
            )

            central_header = _CENTRAL_HEADER_STRUCT.pack(
                _CENTRAL_SIG,
                _VERSION_MADE_BY_UNIX,
                _ZIP_VERSION,
                0,          # flags
                0,          # method = ZIP_STORED
                dos_time,
                dos_date,
                crc,
                size,
                size,
                len(name_bytes),
                0,          # extra length
                0,          # comment length
                0,          # disk number start
                0,          # internal file attrs
                _EXTERNAL_ATTR_REGULAR_FILE,
                offset,
            )
            central_records.append(central_header + name_bytes)

            yield local_header + name_bytes + data
            offset += len(local_header) + len(name_bytes) + size

        central_dir = b"".join(central_records)
        count = len(entries)
        eocd = _EOCD_STRUCT.pack(
            _EOCD_SIG,
            0,
            0,
            count,
            count,
            len(central_dir),
            offset,
            0,
        )
        yield central_dir + eocd
    finally:
        if cleanup_dir is not None:
            shutil.rmtree(cleanup_dir, ignore_errors=True)

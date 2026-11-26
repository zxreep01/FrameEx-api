from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from app.extractor import resolve_ffmpeg_binary


@pytest.fixture(scope="session")
def sample_media(tmp_path_factory: pytest.TempPathFactory) -> dict[str, bytes]:
    """Generate compact, deterministic test media assets via FFmpeg once per test session."""
    ffmpeg = resolve_ffmpeg_binary()
    media_dir: Path = tmp_path_factory.mktemp("media")

    mp4_path = media_dir / "sample_2s.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=2.0:size=160x120:rate=20",
            "-pix_fmt",
            "yuv420p",
            str(mp4_path),
        ],
        check=True,
        capture_output=True,
    )

    gif_path = media_dir / "anim_1_5s.gif"
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=1.5:size=120x90:rate=10",
            str(gif_path),
        ],
        check=True,
        capture_output=True,
    )

    static_gif_path = media_dir / "static.gif"
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=0.1:size=100x80:rate=10",
            "-frames:v",
            "1",
            str(static_gif_path),
        ],
        check=True,
        capture_output=True,
    )

    wav_path = media_dir / "audio_only.wav"
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.5",
            str(wav_path),
        ],
        check=True,
        capture_output=True,
    )

    return {
        "mp4": mp4_path.read_bytes(),
        "gif": gif_path.read_bytes(),
        "static_gif": static_gif_path.read_bytes(),
        "wav": wav_path.read_bytes(),
    }

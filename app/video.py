"""ffmpeg transcode for web/iPhone delivery + poster frame + probe."""

import json
import logging
import subprocess

log = logging.getLogger("mise.video")


def _run(cmd: list[str], timeout: int = 1800) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed ({proc.returncode}): {proc.stderr[-400:]}")


def probe(path: str) -> dict:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=width,height,codec_type",
            "-of",
            "json",
            path,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {proc.stderr[-400:]}")
    info = json.loads(proc.stdout)
    vid = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
    return {
        "duration": float(info.get("format", {}).get("duration") or 0),
        "width": vid.get("width"),
        "height": vid.get("height"),
    }


def transcode(src: str, dst_mp4: str, poster_jpg: str, max_w: int, crf: int) -> dict:
    """H.264 yuv420p 8-bit + AAC + faststart, even dims — iPhone Safari safe."""
    _run(
        [
            "ffmpeg",
            "-y",
            "-i",
            src,
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            str(crf),
            "-pix_fmt",
            "yuv420p",
            "-vf",
            f"scale='min({max_w},iw)':-2",
            "-movflags",
            "+faststart",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-map_metadata",
            "-1",
            dst_mp4,
        ]
    )
    _run(
        ["ffmpeg", "-y", "-ss", "1", "-i", dst_mp4, "-frames:v", "1", "-q:v", "3", poster_jpg],
        timeout=300,
    )
    return probe(dst_mp4)

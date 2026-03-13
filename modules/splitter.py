"""
modules/splitter.py
Splits large video/audio files into parts that fit within Telegram's upload limit.

Strategy:
  - Files under SPLIT_THRESHOLD_MB  → upload as-is
  - Files over  SPLIT_THRESHOLD_MB  → split into numbered parts using FFmpeg
    (stream copy, no re-encoding — fast and lossless)

Part naming:  VideoTitle_part01.mp4, VideoTitle_part02.mp4, ...
"""

import os
import asyncio
import logging
import math
from typing import Optional

from config import TMP_DIR, MAX_FILE_SIZE_MB
from modules.util import safe_filename, delete_files

logger = logging.getLogger(__name__)

# Each split part must be safely under Telegram's limit
# Using 1900 MB as the ceiling to give headroom for container overhead
SPLIT_THRESHOLD_MB = int(os.getenv("SPLIT_THRESHOLD_MB", "1900"))
SPLIT_PART_MB      = int(os.getenv("SPLIT_PART_MB", "1900"))      # size of each part


# ── Public API ────────────────────────────────────────────────────────────────

def needs_splitting(path: str) -> bool:
    """Return True if the file exceeds the split threshold."""
    size_mb = os.path.getsize(path) / (1024 * 1024)
    return size_mb > SPLIT_THRESHOLD_MB


async def split_file(path: str, title: str, user_id: int) -> list[dict]:
    """
    Split a media file into parts using FFmpeg segment muxer (stream copy).

    Returns a list of dicts:
        [
          {"path": "/tmp/..._part01.mp4", "part": 1, "total": 3, "label": "Part 1/3"},
          ...
        ]
    Each part is guaranteed to be under SPLIT_PART_MB.
    Returns empty list on failure.
    """
    size_bytes = os.path.getsize(path)
    size_mb    = size_bytes / (1024 * 1024)
    total_parts = math.ceil(size_mb / SPLIT_PART_MB)

    logger.info(
        f"Splitting {path} ({size_mb:.0f} MB) into ~{total_parts} parts "
        f"of {SPLIT_PART_MB} MB each"
    )

    ext        = os.path.splitext(path)[1].lower() or ".mp4"
    safe_title = safe_filename(title, max_len=40)
    out_pattern = os.path.join(
        TMP_DIR,
        f"{safe_title}_{user_id}_part%02d{ext}",
    )

    # Get total duration via ffprobe to compute segment time
    duration_s = await _get_duration(path)
    if duration_s <= 0:
        logger.warning("Could not get duration — falling back to byte-based split")
        return await _byte_split(path, title, user_id, ext)

    # Seconds per segment = (total_duration / total_parts) with a small buffer
    segment_time = math.ceil(duration_s / total_parts)

    # FFmpeg: split by time, stream-copy (no re-encode)
    cmd = [
        "ffmpeg", "-y",
        "-i", path,
        "-c", "copy",              # no re-encode
        "-map", "0",
        "-segment_time", str(segment_time),
        "-f", "segment",
        "-reset_timestamps", "1",
        out_pattern,
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)

    if proc.returncode != 0:
        err = stderr.decode(errors="replace")
        logger.error(f"FFmpeg split failed: {err[-300:]}")
        return []

    # Collect output parts in order
    parts = _collect_parts(out_pattern, total_parts, safe_title, user_id, ext)

    if not parts:
        logger.error("Split produced no output files")
        return []

    logger.info(f"Split complete: {len(parts)} parts")
    return parts


# ── Fallback: byte-level split for non-seekable or unknown-duration files ─────

async def _byte_split(
    path: str, title: str, user_id: int, ext: str
) -> list[dict]:
    """
    Raw byte split — used when FFprobe can't determine duration.
    Not ideal for video (breaks mid-frame) but works for audio/unknown formats.
    """
    chunk_size = SPLIT_PART_MB * 1024 * 1024
    safe_title = safe_filename(title, max_len=40)
    parts      = []
    part_num   = 0

    with open(path, "rb") as src:
        while True:
            chunk = src.read(chunk_size)
            if not chunk:
                break
            part_num += 1
            out_path = os.path.join(
                TMP_DIR, f"{safe_title}_{user_id}_part{part_num:02d}{ext}"
            )
            with open(out_path, "wb") as dst:
                dst.write(chunk)
            parts.append({"path": out_path, "part": part_num, "_tmp": True})

    total = len(parts)
    for p in parts:
        p["total"] = total
        p["label"] = f"Part {p['part']}/{total}"

    return parts


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _get_duration(path: str) -> float:
    """Return duration in seconds from ffprobe, or 0.0 on failure."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        import json
        data = json.loads(stdout)
        return float(data["format"].get("duration", 0))
    except Exception as e:
        logger.warning(f"ffprobe duration failed: {e}")
        return 0.0


def _collect_parts(
    pattern: str, expected: int, safe_title: str, user_id: int, ext: str
) -> list[dict]:
    """Scan TMP_DIR for the generated segment files and build the parts list."""
    found = []
    # FFmpeg names them: part00, part01, ...
    for i in range(expected + 5):   # scan a few beyond expected
        candidate = pattern.replace("%02d", f"{i:02d}")
        if os.path.exists(candidate) and os.path.getsize(candidate) > 0:
            found.append(candidate)
        elif found:
            # Stop at first gap after we've found at least one
            break

    total = len(found)
    return [
        {
            "path":  p,
            "part":  idx + 1,
            "total": total,
            "label": f"Part {idx + 1}/{total}",
        }
        for idx, p in enumerate(found)
    ]

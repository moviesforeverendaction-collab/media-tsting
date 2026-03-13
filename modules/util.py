"""
modules/util.py
Utility functions: URL validation, file helpers, formatting.
"""

import asyncio
import json
import logging
import os
import re
import shutil
from pathlib import Path
from urllib.parse import urlparse

from config import TMP_DIR, SUPPORTED_DOMAINS, MAX_FILE_SIZE_MB

logger = logging.getLogger(__name__)

_URL_RE = re.compile(
    r"https?://"
    r"(?:[-\w.]|(?:%[\da-fA-F]{2}))+"
    r"(?:[/?#]\S*)?",
    re.IGNORECASE,
)


def extract_urls(text: str) -> list[str]:
    return _URL_RE.findall(text)


def is_supported_url(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower().lstrip("www.")
        return any(domain in host for domain in SUPPORTED_DOMAINS)
    except Exception:
        return False


# ── File helpers ──────────────────────────────────────────────────────────────

def ensure_tmp_dir():
    os.makedirs(TMP_DIR, exist_ok=True)


def safe_filename(name: str, max_len: int = 60) -> str:
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    name = name.strip(". ")
    return name[:max_len]


def file_size_mb(path: str) -> float:
    try:
        return os.path.getsize(path) / (1024 * 1024)
    except OSError:
        return 0.0


def is_within_size_limit(path: str) -> bool:
    return file_size_mb(path) <= MAX_FILE_SIZE_MB


def delete_files(*paths: str):
    for path in paths:
        try:
            if path and os.path.exists(path):
                os.remove(path)
                logger.debug(f"Deleted: {path}")
        except OSError as e:
            logger.warning(f"Could not delete {path}: {e}")


def cleanup_dir(dirpath: str):
    """Remove an entire temp directory and all its contents."""
    try:
        if dirpath and os.path.isdir(dirpath):
            shutil.rmtree(dirpath, ignore_errors=True)
            logger.debug(f"Cleaned dir: {dirpath}")
    except Exception as e:
        logger.warning(f"cleanup_dir error ({dirpath}): {e}")


def cleanup_user_tmp(user_id: int):
    """Delete all tmp directories/files that belong to a user."""
    pattern = f"_{user_id}_"
    try:
        for entry in Path(TMP_DIR).iterdir():
            if pattern in entry.name:
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)
    except Exception as e:
        logger.warning(f"cleanup_user_tmp error: {e}")


# ── Thumbnail extraction ──────────────────────────────────────────────────────

async def extract_thumbnail(video_path: str, user_id: int) -> str | None:
    """Extract a frame at 1 second as JPEG thumbnail (320px wide)."""
    thumb_path = os.path.join(TMP_DIR, f"thumb_{user_id}_{os.getpid()}.jpg")
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y",
            "-i", video_path,
            "-ss", "00:00:01",
            "-vframes", "1",
            "-vf", "scale=320:-1",
            thumb_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.communicate(), timeout=30)
        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
            return thumb_path
    except Exception as e:
        logger.warning(f"Thumbnail extraction failed: {e}")
    return None


# ── Video duration ────────────────────────────────────────────────────────────

async def get_video_duration(path: str) -> int:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        data = json.loads(stdout)
        return int(float(data["format"].get("duration", 0)))
    except Exception:
        return 0


# ── Formatting ────────────────────────────────────────────────────────────────

def format_size(bytes_val: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if bytes_val < 1024:
            return f"{bytes_val:.1f} {unit}"
        bytes_val /= 1024
    return f"{bytes_val:.1f} TB"


def format_duration(seconds) -> str:
    seconds = int(seconds or 0)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def progress_bar(current: int, total: int, width: int = 16) -> str:
    if total == 0:
        return "░" * width
    ratio  = min(current / total, 1.0)
    filled = ratio * width
    full   = int(filled)
    frac   = filled - full
    sub    = " ▏▎▍▌▋▊▉█"[int(frac * 8)]
    bar    = "█" * full + sub + "░" * max(width - full - 1, 0)
    return bar[:width]
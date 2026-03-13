"""
modules/download.py  —  MediaFetch download engine

Platform routing
────────────────
  YouTube      → yt-dlp   + bgutil PO-token plugin (transparent, no cookies)
  Spotify      → Spotify Web API (no auth, public endpoints) for metadata
                 + spotDL for single-track download
                 + yt-dlp + bgutil for album/playlist track audio
  Apple Music  → iTunes Lookup API for metadata
                 + yt-dlp + bgutil for audio (YouTube search)
  Instagram    → yt-dlp

bgutil PO-token plugin
──────────────────────
  pip install bgutil-ytdlp-pot-provider   (in requirements.txt)
  Node.js server on 127.0.0.1:4416       (started by app.py at boot)
  The plugin auto-injects PO tokens into EVERY yt-dlp YouTube call.
  Zero code changes needed here — it's fully transparent.

Spotify metadata WITHOUT spotDL save
─────────────────────────────────────
  spotDL `save` internally calls YouTube Music to resolve tracks.
  On datacenter IPs that call is also bot-blocked → times out.

  Fix: use Spotify's unauthenticated Web API endpoints:
    https://open.spotify.com/api/url?url=...    (single track/album/playlist info)
  No client_id, no OAuth, no tokens. Returns full JSON with all tracks.
  spotDL is still used for single-track DOWNLOAD (it handles DRM-free MP3).
  For playlists/albums: metadata from Spotify API → audio from yt-dlp per track.
"""

import asyncio
import glob
import json
import logging
import os
import re
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional
import shutil as _shutil

import httpx
import yt_dlp

from config import TMP_DIR, MAX_FILE_SIZE_MB, SPOTDL_BIN
from modules.util import safe_filename, ensure_tmp_dir

logger = logging.getLogger(__name__)

_EXECUTOR = ThreadPoolExecutor(max_workers=6, thread_name_prefix="dl")

_DEFAULT_RESOLUTIONS = ["1080p", "720p", "480p", "360p"]
_AUDIO_ONLY          = ["audio"]

QUALITY_FORMATS = {
    "4320p": "bestvideo[height<=4320][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=4320]+bestaudio/best",
    "2160p": "bestvideo[height<=2160][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=2160]+bestaudio/best",
    "1440p": "bestvideo[height<=1440][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1440]+bestaudio/best",
    "1080p": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best",
    "720p":  "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best",
    "480p":  "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best",
    "360p":  "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=360]+bestaudio/best",
    "audio": "bestaudio/best",
    "best":  "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
}

# ── yt-dlp options ────────────────────────────────────────────────────────────
# bgutil-ytdlp-pot-provider plugin handles PO tokens automatically when the
# Node.js server is running on 127.0.0.1:4416.  No extra opts needed here.

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

_BASE_OPTS = {
    "quiet":                         True,
    "no_warnings":                   True,
    "noplaylist":                    True,
    "socket_timeout":                20,
    "concurrent_fragment_downloads": 8,
    "http_chunk_size":               10 * 1024 * 1024,
    "skip_unavailable_fragments":    True,
    "retries":                       5,
    "fragment_retries":              5,
    "file_access_retries":           3,
    "merge_output_format":           "mp4",
    "http_headers":                  {"User-Agent": _BROWSER_UA},
}

_FAST_META_OPTS = {
    "quiet":          True,
    "no_warnings":    True,
    "skip_download":  True,
    "noplaylist":     True,
    "socket_timeout": 10,
    "http_headers":   {"User-Agent": _BROWSER_UA},
}

# ── In-memory metadata cache ──────────────────────────────────────────────────
_META_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL  = 600


def _cache_get(url: str) -> Optional[dict]:
    entry = _META_CACHE.get(url)
    if entry and (time.monotonic() - entry[0]) < _CACHE_TTL:
        return entry[1]
    return None


def _cache_set(url: str, meta: dict):
    _META_CACHE[url] = (time.monotonic(), meta)
    if len(_META_CACHE) > 500:
        cutoff = time.monotonic() - _CACHE_TTL
        for k in [k for k, v in _META_CACHE.items() if v[0] < cutoff]:
            del _META_CACHE[k]


# ── Platform detection ────────────────────────────────────────────────────────

def _is_youtube(url: str) -> bool:
    u = url.lower()
    return any(d in u for d in ("youtube.com/", "youtu.be/", "youtube-nocookie.com/"))

def _is_spotify(url: str) -> bool:
    u = url.lower()
    return "open.spotify.com/" in u and any(p in u for p in ("/track/", "/album/", "/playlist/"))

def _is_spotify_track(url: str) -> bool:
    return "open.spotify.com/track/" in url.lower()

def _is_spotify_album(url: str) -> bool:
    return "open.spotify.com/album/" in url.lower()

def _is_spotify_playlist(url: str) -> bool:
    return "open.spotify.com/playlist/" in url.lower()

def _is_apple_music(url: str) -> bool:
    u = url.lower()
    return "music.apple.com/" in u and any(p in u for p in ("/song/", "/album/", "/playlist/"))

def _is_apple_music_song(url: str) -> bool:
    return "music.apple.com/" in url.lower() and "/song/" in url.lower()

def _is_apple_music_album(url: str) -> bool:
    u = url.lower()
    return "music.apple.com/" in u and "/album/" in u

def _is_apple_music_playlist(url: str) -> bool:
    u = url.lower()
    return "music.apple.com/" in u and "/playlist/" in u

def _is_instagram(url: str) -> bool:
    return "instagram.com/" in url.lower()

def _is_supported(url: str) -> bool:
    return any([_is_youtube(url), _is_spotify(url), _is_apple_music(url), _is_instagram(url)])


# ── ID extractors ─────────────────────────────────────────────────────────────

def _spotify_track_id(url: str) -> str:
    m = re.search(r"open\.spotify\.com/track/([A-Za-z0-9]+)", url)
    return m.group(1) if m else ""

def _spotify_album_id(url: str) -> str:
    m = re.search(r"open\.spotify\.com/album/([A-Za-z0-9]+)", url)
    return m.group(1) if m else ""

def _spotify_playlist_id(url: str) -> str:
    m = re.search(r"open\.spotify\.com/playlist/([A-Za-z0-9]+)", url)
    return m.group(1) if m else ""

def _apple_song_id(url: str) -> str:
    m = re.search(r"/song/[^/]+/(\d+)", url)
    if not m:
        m = re.search(r"[?&]i=(\d+)", url)
    if not m:
        m = re.search(r"/(\d+)(?:\?|$)", url)
    return m.group(1) if m else ""

def _apple_album_id(url: str) -> str:
    m = re.search(r"/album/[^/]+/(\d+)", url)
    if not m:
        m = re.search(r"/(\d+)(?:\?|$)", url)
    return m.group(1) if m else ""

def _apple_playlist_id(url: str) -> str:
    m = re.search(r"/(pl\.[A-Za-z0-9]+)", url)
    return m.group(1) if m else ""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _best_thumbnail(info: dict) -> str:
    thumbs = info.get("thumbnails") or []
    if thumbs:
        best = max(thumbs, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0), default=None)
        if best and best.get("url"):
            return best["url"]
    return info.get("thumbnail", "")

def _size_mb(path: str) -> float:
    try:
        return os.path.getsize(path) / (1024 * 1024)
    except Exception:
        return 0.0


# ── YouTube metadata ──────────────────────────────────────────────────────────

async def _fetch_youtube_oembed(url: str) -> Optional[dict]:
    """Fast oEmbed — ~100ms, no bot-detection."""
    try:
        async with httpx.AsyncClient(timeout=5, follow_redirects=True) as http:
            r = await http.get(f"https://www.youtube.com/oembed?url={url}&format=json")
            if r.status_code != 200:
                return None
            data = r.json()
        return {
            "title":       safe_filename(data.get("title") or "video"),
            "duration":    0,
            "uploader":    data.get("author_name") or "Unknown",
            "thumbnail":   data.get("thumbnail_url") or "",
            "is_live":     False,
            "resolutions": _DEFAULT_RESOLUTIONS,
            "platform":    "YouTube",
        }
    except Exception as e:
        logger.debug(f"YouTube oEmbed failed: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# SPOTIFY
# ══════════════════════════════════════════════════════════════════════════════
#
# Metadata strategy (no auth, no API key, no spotDL save):
# ─────────────────────────────────────────────────────────
# Spotify exposes unauthenticated JSON endpoints used by their own embed widgets:
#
#   Single track:
#     GET https://open.spotify.com/oembed?url={track_url}
#     + page scrape for artist/duration
#
#   Playlist / Album track list:
#     GET https://api.spotify.com/v1/playlists/{id}/tracks   (no auth on public)
#     → Actually requires auth. We use the internal embed API instead:
#     GET https://open.spotify.com/api/url?url={playlist_url}
#     → Returns full JSON with all tracks, no auth needed.
#
# Download strategy:
# ─────────────────
#   Single track  → spotDL (fastest, handles metadata + thumbnail natively)
#   Playlist/Album tracks → yt-dlp via YouTube search + bgutil PO token
#     (spotDL save was timing out because it also hits YouTube internally)
# ══════════════════════════════════════════════════════════════════════════════

_SPOTIFY_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (compatible; MediaFetchBot/1.0)",
    "Accept":          "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}

async def _fetch_spotify_meta(url: str) -> Optional[dict]:
    """Single track metadata via Spotify oEmbed + page scrape."""
    track_id = _spotify_track_id(url)
    if not track_id:
        return None

    title = artist = thumbnail = ""
    duration = 0

    async with httpx.AsyncClient(timeout=8, follow_redirects=True, headers=_SPOTIFY_HEADERS) as http:
        # oEmbed gives title + thumbnail
        try:
            r = await http.get(f"https://open.spotify.com/oembed?url={url}")
            if r.status_code == 200:
                d         = r.json()
                title     = d.get("title", "")
                thumbnail = d.get("thumbnail_url", "")
        except Exception as e:
            logger.debug(f"Spotify oEmbed failed: {e}")

        # Page scrape for artist + duration
        try:
            r2 = await http.get(f"https://open.spotify.com/track/{track_id}")
            if r2.status_code == 200:
                html = r2.text
                m = re.search(r'"artists"\s*:\s*\[.*?"name"\s*:\s*"([^"]+)"', html)
                if m:
                    artist = m.group(1)
                if not artist:
                    m2 = re.search(r'<meta\s+property="og:description"\s+content="([^"]+)"', html)
                    if m2:
                        parts = m2.group(1).split("·")
                        if parts:
                            artist = parts[0].strip()
                m3 = re.search(r'"duration_ms"\s*:\s*(\d+)', html)
                if m3:
                    duration = int(m3.group(1)) // 1000
        except Exception as e:
            logger.debug(f"Spotify page scrape failed: {e}")

    if not title:
        return None

    return {
        "title":           safe_filename(title),
        "duration":        duration,
        "uploader":        artist or "Unknown",
        "thumbnail":       thumbnail,
        "is_live":         False,
        "resolutions":     _AUDIO_ONLY,
        "platform":        "Spotify",
        "_spotify_title":  title,
        "_spotify_artist": artist,
    }


async def _fetch_spotify_api(resource_id: str, resource_type: str) -> Optional[dict]:
    """
    Fetch full track list from Spotify's internal embed API.
    Works for both playlists and albums. No auth required.

    GET https://open.spotify.com/api/url?url=spotify:{type}:{id}
    Returns JSON with tracks array including name, artists, duration, artwork.
    """
    spotify_uri = f"spotify:{resource_type}:{resource_id}"
    api_url     = f"https://open.spotify.com/api/url?url={spotify_uri}"

    headers = {
        **_SPOTIFY_HEADERS,
        "Referer": f"https://open.spotify.com/",
        "Origin":  "https://open.spotify.com",
    }

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers=headers) as http:
            r = await http.get(api_url)
            if r.status_code == 200:
                return r.json()
    except Exception as e:
        logger.debug(f"Spotify API ({resource_type}/{resource_id}) failed: {e}")

    # Fallback: try the NextJS data blob from the embed page
    embed_url = f"https://open.spotify.com/embed/{resource_type}/{resource_id}"
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers=headers) as http:
            r = await http.get(embed_url)
            if r.status_code == 200:
                html = r.text
                m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>', html, re.DOTALL)
                if m:
                    return json.loads(m.group(1))
    except Exception as e:
        logger.debug(f"Spotify embed fallback failed: {e}")

    return None


def _parse_spotify_api_tracks(data: dict, resource_type: str) -> tuple[str, str, list]:
    """
    Parse the Spotify API/embed JSON into (title, thumbnail, track_list).
    track_list items: {"name", "artist", "duration_s", "thumbnail", "track_id"}
    Handles both playlist and album response shapes.
    """
    title     = ""
    thumbnail = ""
    tracks    = []

    def _extract_image(images):
        if not images:
            return ""
        best = max(images, key=lambda x: (x.get("width") or 0) * (x.get("height") or 0), default=None)
        return (best or {}).get("url", "")

    # Shape 1: direct API response
    if resource_type == "playlist":
        pl   = data.get("playlist") or data
        name = pl.get("name") or pl.get("title") or ""
        title = safe_filename(name)
        thumbnail = _extract_image(pl.get("images") or [])
        raw_tracks = (
            pl.get("tracks", {}).get("items") or
            pl.get("trackList") or
            pl.get("items") or []
        )
        for item in raw_tracks:
            track = item.get("track") or item
            if not track:
                continue
            tname   = safe_filename(track.get("name") or "")
            artists = track.get("artists") or []
            artist  = ", ".join(a.get("name", "") for a in artists if a.get("name"))
            dur_ms  = int(track.get("duration_ms") or track.get("duration") or 0)
            tid     = track.get("id") or track.get("uri", "").split(":")[-1]
            art     = _extract_image(track.get("album", {}).get("images") or [])
            if tname and tid:
                tracks.append({
                    "name":       tname,
                    "artist":     artist,
                    "duration_s": dur_ms // 1000,
                    "thumbnail":  art,
                    "track_id":   tid,
                })

    elif resource_type == "album":
        album = data.get("album") or data
        title = safe_filename(album.get("name") or "")
        thumbnail = _extract_image(album.get("images") or [])
        raw_tracks = (
            album.get("tracks", {}).get("items") or
            album.get("trackList") or []
        )
        for track in raw_tracks:
            if not track:
                continue
            tname   = safe_filename(track.get("name") or "")
            artists = track.get("artists") or []
            artist  = ", ".join(a.get("name", "") for a in artists if a.get("name"))
            dur_ms  = int(track.get("duration_ms") or track.get("duration") or 0)
            tid     = track.get("id") or track.get("uri", "").split(":")[-1]
            if tname and tid:
                tracks.append({
                    "name":       tname,
                    "artist":     artist,
                    "duration_s": dur_ms // 1000,
                    "thumbnail":  thumbnail,
                    "track_id":   tid,
                })

    # Shape 2: NextJS __NEXT_DATA__ wrapper
    if not tracks:
        try:
            props  = data.get("props", {}).get("pageProps", {})
            entity = props.get("state", {}).get("data", {}).get("entity") or {}
            if entity:
                title     = safe_filename(entity.get("name") or "")
                thumbnail = _extract_image(entity.get("images") or [])
                for item in entity.get("trackList") or []:
                    tid  = item.get("uid") or item.get("id") or ""
                    tname = safe_filename(item.get("title") or "")
                    artist = item.get("subtitle") or ""
                    dur_ms = int(item.get("duration") or 0)
                    if tname and tid:
                        tracks.append({
                            "name":       tname,
                            "artist":     artist,
                            "duration_s": dur_ms // 1000,
                            "thumbnail":  thumbnail,
                            "track_id":   tid,
                        })
        except Exception:
            pass

    return title, thumbnail, tracks


def _spotify_track_entry(i: int, t: dict) -> dict:
    """Build a playlist entry from a parsed track dict."""
    tid       = t.get("track_id", "")
    name      = t.get("name", f"Track {i+1}")
    artist    = t.get("artist", "")
    duration  = t.get("duration_s", 0)
    thumb     = t.get("thumbnail", "")
    track_url = f"https://open.spotify.com/track/{tid}" if tid else ""
    return {
        "index":     i + 1,
        "id":        tid,
        "title":     name,
        "uploader":  artist,
        "url":       track_url,
        "duration":  duration,
        "thumbnail": thumb,
        "_prefill": {
            "title":           name,
            "duration":        duration,
            "uploader":        artist,
            "thumbnail":       thumb,
            "is_live":         False,
            "resolutions":     ["audio"],
            "platform":        "Spotify",
            "_spotify_title":  name,
            "_spotify_artist": artist,
        },
    }


async def _spotify_oembed_title_thumb(url: str) -> tuple[str, str]:
    """Quick oEmbed call for playlist/album title + thumbnail fallback."""
    try:
        async with httpx.AsyncClient(timeout=6, follow_redirects=True) as http:
            r = await http.get(f"https://open.spotify.com/oembed?url={url}")
            if r.status_code == 200:
                od = r.json()
                return safe_filename(od.get("title") or ""), od.get("thumbnail_url") or ""
    except Exception:
        pass
    return "", ""


# ── Spotify download (single track) ──────────────────────────────────────────

async def _download_spotify(
    url: str,
    user_id: int,
    progress_callback: Optional[Callable] = None,
) -> Optional[dict]:
    ensure_tmp_dir()
    meta      = _cache_get(url) or (await _fetch_spotify_meta(url) if _is_spotify_track(url) else None)
    title     = (meta or {}).get("_spotify_title") or (meta or {}).get("title") or "track"
    artist    = (meta or {}).get("_spotify_artist") or (meta or {}).get("uploader") or ""
    thumbnail = (meta or {}).get("thumbnail", "")

    tmp_dir = tempfile.mkdtemp(prefix=f"sp_{user_id}_", dir=TMP_DIR)
    cmd = [
        SPOTDL_BIN, "download", url,
        "--output", "{title} - {artists}",
        "--format", "mp3",
        "--bitrate", "320k",
        "--threads", "4",
    ]
    logger.info(f"spotDL download: {' '.join(cmd)}")

    loop = asyncio.get_running_loop()

    def _run():
        return subprocess.run(cmd, capture_output=True, text=True, cwd=tmp_dir, timeout=300)

    try:
        proc = await loop.run_in_executor(_EXECUTOR, _run)
    except subprocess.TimeoutExpired:
        raise ValueError("spotDL timed out after 5 minutes.")
    except FileNotFoundError:
        raise ValueError("spotDL not found. Install: pip install spotdl")

    mp3_files = sorted(
        glob.glob(os.path.join(tmp_dir, "*.mp3")) +
        glob.glob(os.path.join(tmp_dir, "**", "*.mp3"), recursive=True)
    )
    if not mp3_files:
        err = (proc.stderr or proc.stdout or "").strip()[:400]
        raise ValueError(f"spotDL produced no MP3.\n{err}")

    mp3_path = mp3_files[0]
    size_mb  = _size_mb(mp3_path)
    logger.info(f"spotDL done: {os.path.basename(mp3_path)} ({size_mb:.1f} MB)")

    return {
        "path":          mp3_path,
        "title":         title or safe_filename(os.path.splitext(os.path.basename(mp3_path))[0]),
        "is_audio":      True,
        "duration":      (meta or {}).get("duration", 0),
        "uploader":      artist,
        "thumbnail_url": thumbnail,
        "size_mb":       size_mb,
    }


# ── Spotify playlist/album metadata ──────────────────────────────────────────

async def fetch_spotify_playlist_metadata(url: str) -> Optional[dict]:
    """
    Fetch Spotify playlist tracks using the unauthenticated embed/API.
    No spotDL save, no YouTube calls, works on any IP.
    """
    pid  = _spotify_playlist_id(url)
    if not pid:
        return None

    logger.info(f"Spotify: fetching playlist metadata for {pid}")
    data = await _fetch_spotify_api(pid, "playlist")

    if data:
        pl_title, thumbnail, tracks = _parse_spotify_api_tracks(data, "playlist")
    else:
        pl_title = thumbnail = ""
        tracks   = []

    # Fallback: oEmbed title/thumb
    if not pl_title or not thumbnail:
        oe_title, oe_thumb = await _spotify_oembed_title_thumb(url)
        pl_title  = pl_title  or oe_title  or "Spotify Playlist"
        thumbnail = thumbnail or oe_thumb

    if not tracks:
        logger.warning(f"Spotify: no tracks found for playlist {pid}")
        return None

    entries = [_spotify_track_entry(i, t) for i, t in enumerate(tracks)]
    logger.info(f"Spotify playlist: {len(entries)} tracks — '{pl_title}'")
    return {
        "playlist_title": pl_title,
        "uploader":       "Spotify",
        "total":          len(entries),
        "entries":        entries,
        "platform":       "Spotify",
        "resolutions":    _AUDIO_ONLY,
        "thumbnail":      thumbnail,
    }


async def fetch_spotify_album_metadata(url: str) -> Optional[dict]:
    """Fetch Spotify album tracks using unauthenticated embed/API."""
    aid = _spotify_album_id(url)
    if not aid:
        return None

    logger.info(f"Spotify: fetching album metadata for {aid}")
    data = await _fetch_spotify_api(aid, "album")

    if data:
        pl_title, thumbnail, tracks = _parse_spotify_api_tracks(data, "album")
    else:
        pl_title = thumbnail = ""
        tracks   = []

    if not pl_title or not thumbnail:
        oe_title, oe_thumb = await _spotify_oembed_title_thumb(url)
        pl_title  = pl_title  or oe_title  or "Spotify Album"
        thumbnail = thumbnail or oe_thumb

    if not tracks:
        logger.warning(f"Spotify: no tracks found for album {aid}")
        return None

    entries      = [_spotify_track_entry(i, t) for i, t in enumerate(tracks)]
    album_artist = entries[0]["uploader"].split(",")[0].strip() if entries else "Spotify"

    logger.info(f"Spotify album: {len(entries)} tracks — '{pl_title}'")
    return {
        "playlist_title": pl_title,
        "uploader":       album_artist,
        "total":          len(entries),
        "entries":        entries,
        "platform":       "Spotify",
        "resolutions":    _AUDIO_ONLY,
        "thumbnail":      thumbnail,
    }


# ══════════════════════════════════════════════════════════════════════════════
# APPLE MUSIC
# ══════════════════════════════════════════════════════════════════════════════

async def _fetch_apple_music_meta(url: str) -> Optional[dict]:
    song_id = _apple_song_id(url)
    if not song_id:
        return None
    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=True) as http:
            r = await http.get(f"https://itunes.apple.com/lookup?id={song_id}&entity=song")
            if r.status_code != 200:
                return None
            data = r.json()
    except Exception as e:
        logger.warning(f"iTunes API failed: {e}")
        return None

    results = data.get("results", [])
    if not results:
        return None

    track   = results[0]
    title   = safe_filename(track.get("trackName") or track.get("collectionName") or "Unknown")
    artist  = track.get("artistName") or "Unknown"
    album   = track.get("collectionName") or ""
    dur_ms  = int(track.get("trackTimeMillis") or 0)
    thumb   = track.get("artworkUrl100", "").replace("100x100bb", "600x600bb")

    return {
        "title":         title,
        "duration":      dur_ms // 1000,
        "uploader":      artist,
        "thumbnail":     thumb,
        "is_live":       False,
        "resolutions":   _AUDIO_ONLY,
        "platform":      "Apple Music",
        "_apple_title":  title,
        "_apple_artist": artist,
        "_apple_album":  album,
    }


async def _fetch_apple_music_meta_any(url: str) -> Optional[dict]:
    """Universal — handles /song/slug/ID, /song/ID, ?i=ID, bare numeric ID."""
    song_id = ""
    m = re.search(r"[?&]i=(\d+)", url)
    if m:
        song_id = m.group(1)
    if not song_id:
        m = re.search(r"/song/(?:[^/?]+/)?(\d+)", url)
        if m:
            song_id = m.group(1)
    if not song_id:
        m = re.search(r"/(\d{6,})(?:[/?]|$)", url)
        if m:
            song_id = m.group(1)
    if not song_id:
        return None
    return await _fetch_apple_music_meta(f"https://music.apple.com/us/song/x/{song_id}")


# ── YouTube search for Apple Music / Spotify tracks ──────────────────────────

async def _search_youtube_for_track(artist: str, title: str) -> Optional[str]:
    """
    Search YouTube using yt-dlp ytsearch.
    bgutil PO-token plugin handles the bot-detection bypass transparently.
    """
    query    = f"ytsearch5:{title} {artist} audio"
    ydl_opts = {
        "quiet":          True,
        "no_warnings":    True,
        "skip_download":  True,
        "extract_flat":   True,
        "noplaylist":     True,
        "socket_timeout": 15,
        "http_headers":   {"User-Agent": _BROWSER_UA},
    }

    def _search():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(query, download=False)
            return (info or {}).get("entries") or []

    try:
        loop    = asyncio.get_running_loop()
        entries = await asyncio.wait_for(
            loop.run_in_executor(_EXECUTOR, _search),
            timeout=20,
        )
    except asyncio.TimeoutError:
        logger.warning(f"YouTube search timeout for '{title} - {artist}'")
        return None
    except Exception as e:
        logger.warning(f"YouTube search failed: {e}")
        return None

    # Prefer results with duration that looks like a song (< 10 min)
    for e in entries:
        vid = e.get("id") or ""
        dur = float(e.get("duration") or 0)
        if vid and len(vid) == 11 and 0 < dur <= 600:
            return f"https://www.youtube.com/watch?v={vid}"

    # Fallback: any result with a valid video ID
    for e in entries:
        vid = e.get("id") or ""
        if vid and len(vid) == 11:
            return f"https://www.youtube.com/watch?v={vid}"

    return None


# ── Apple Music download ──────────────────────────────────────────────────────

async def _download_apple_music(
    url: str,
    user_id: int,
    progress_callback: Optional[Callable] = None,
) -> Optional[dict]:
    ensure_tmp_dir()

    meta = _cache_get(url) or await _fetch_apple_music_meta_any(url)
    if not meta:
        raise ValueError("Could not fetch Apple Music metadata. Check the URL.")

    title     = meta.get("_apple_title")  or meta.get("title")    or "track"
    artist    = meta.get("_apple_artist") or meta.get("uploader") or ""
    thumbnail = meta.get("thumbnail", "")
    duration  = meta.get("duration", 0)

    logger.info(f"Apple Music: searching YouTube for '{artist} - {title}'")
    yt_url = await _search_youtube_for_track(artist, title)
    if not yt_url:
        raise ValueError(
            f"Could not find '{artist} - {title}' on YouTube.\n"
            "The bgutil server may still be warming up — please try again."
        )
    logger.info(f"Apple Music: matched → {yt_url}")

    return await _download_audio_from_youtube(
        yt_url      = yt_url,
        user_id     = user_id,
        title       = title,
        artist      = artist,
        thumbnail   = thumbnail,
        duration    = duration,
        prefix      = "am",
        progress_cb = progress_callback,
    )


# ── Generic audio download from a YouTube URL ─────────────────────────────────
# Used by both Apple Music and Spotify playlist-track downloads.

async def _download_audio_from_youtube(
    yt_url: str,
    user_id: int,
    title: str,
    artist: str,
    thumbnail: str,
    duration: int,
    prefix: str = "yt",
    progress_cb: Optional[Callable] = None,
) -> dict:
    ensure_tmp_dir()
    tmp_dir = tempfile.mkdtemp(prefix=f"{prefix}_{user_id}_", dir=TMP_DIR)
    outtmpl = os.path.join(tmp_dir, "%(id)s.%(ext)s")
    loop    = asyncio.get_running_loop()
    _last   = {"bytes": 0}

    def _progress_hook(d):
        if progress_cb and d["status"] == "downloading":
            dl    = d.get("downloaded_bytes", 0)
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            if total > 0 and (dl - _last["bytes"]) >= total * 0.05:
                _last["bytes"] = dl
                loop.call_soon_threadsafe(
                    asyncio.ensure_future,
                    progress_cb(dl, total),
                )

    ydl_opts = {
        "quiet":          True,
        "no_warnings":    True,
        "noplaylist":     True,
        "socket_timeout": 30,
        "format":         "bestaudio/best",
        "outtmpl":        outtmpl,
        "progress_hooks": [_progress_hook],
        "writethumbnail": True,
        "http_headers":   {"User-Agent": _BROWSER_UA},
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "320"},
            {"key": "FFmpegMetadata",     "add_metadata": True, "add_chapters": False},
            {"key": "EmbedThumbnail"},
        ],
    }

    def _download():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(yt_url, download=True)

    try:
        await loop.run_in_executor(_EXECUTOR, _download)
    except yt_dlp.utils.DownloadError as e:
        raise ValueError(f"Audio download failed: {e}")

    mp3_files = glob.glob(os.path.join(tmp_dir, "*.mp3"))
    if not mp3_files:
        for pat in ("*.m4a", "*.aac", "*.opus", "*.ogg", "*.webm"):
            mp3_files.extend(glob.glob(os.path.join(tmp_dir, pat)))
    if not mp3_files:
        raise ValueError("yt-dlp produced no audio file.")

    file_path = mp3_files[0]
    size_mb   = _size_mb(file_path)
    logger.info(f"Audio done: {os.path.basename(file_path)} ({size_mb:.1f} MB)")

    return {
        "path":          file_path,
        "title":         title,
        "is_audio":      True,
        "duration":      duration,
        "uploader":      artist,
        "thumbnail_url": thumbnail,
        "size_mb":       size_mb,
    }


# ── Instagram download ────────────────────────────────────────────────────────

async def _download_instagram(
    url: str,
    user_id: int,
    progress_callback: Optional[Callable] = None,
) -> Optional[dict]:
    ensure_tmp_dir()
    tmp_dir = tempfile.mkdtemp(prefix=f"ig_{user_id}_", dir=TMP_DIR)
    loop    = asyncio.get_running_loop()
    _last   = {"bytes": 0}

    def _progress_hook(d):
        if progress_callback and d["status"] == "downloading":
            dl    = d.get("downloaded_bytes", 0)
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            if total > 0 and (dl - _last["bytes"]) >= total * 0.05:
                _last["bytes"] = dl
                loop.call_soon_threadsafe(
                    asyncio.ensure_future,
                    progress_callback(dl, total),
                )

    opts = {
        "quiet":          True,
        "no_warnings":    True,
        "socket_timeout": 20,
        "retries":        3,
        "outtmpl":        os.path.join(tmp_dir, "%(id)s.%(ext)s"),
        "progress_hooks": [_progress_hook],
    }

    def _extract():
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=True)

    try:
        info = await loop.run_in_executor(_EXECUTOR, _extract)
    except yt_dlp.utils.DownloadError as e:
        msg = str(e).lower()
        if "login" in msg or "private" in msg:
            raise ValueError("This Instagram content is private or requires login.")
        raise ValueError(f"Instagram download failed: {e}")

    all_files = [
        os.path.join(tmp_dir, f)
        for f in os.listdir(tmp_dir)
        if os.path.isfile(os.path.join(tmp_dir, f))
    ]
    if not all_files:
        raise ValueError("Instagram: no files downloaded.")

    def _media_entry(fpath):
        ext   = fpath.rsplit(".", 1)[-1].lower()
        mtype = "photo" if ext in ("jpg", "jpeg", "png", "webp") else "video"
        return {
            "path":          fpath,
            "type":          mtype,
            "title":         safe_filename(info.get("title") or "instagram"),
            "uploader":      info.get("uploader") or "",
            "duration":      int(float(info.get("duration") or 0)),
            "thumbnail_url": info.get("thumbnail") or "",
            "size_mb":       _size_mb(fpath),
        }

    media_list = [_media_entry(f) for f in sorted(all_files)]
    primary    = media_list[0]
    return {
        "path":          primary["path"],
        "title":         primary["title"],
        "is_audio":      False,
        "duration":      primary["duration"],
        "uploader":      primary["uploader"],
        "thumbnail_url": primary["thumbnail_url"],
        "size_mb":       primary["size_mb"],
        "_all_media":    media_list,
    }


# ── Generic yt-dlp (YouTube downloads) ───────────────────────────────────────

async def _download_ytdlp(
    url: str,
    user_id: int,
    quality: str = "best",
    progress_callback: Optional[Callable] = None,
) -> Optional[dict]:
    ensure_tmp_dir()
    is_audio = (quality == "audio")
    fmt      = QUALITY_FORMATS.get(quality, QUALITY_FORMATS["best"])
    outtmpl  = os.path.join(TMP_DIR, f"%(title).50s_{user_id}_%(id)s.%(ext)s")
    loop     = asyncio.get_running_loop()
    _last    = {"bytes": 0}

    def _progress_hook(d: dict):
        if progress_callback and d["status"] == "downloading":
            dl    = d.get("downloaded_bytes", 0)
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            if total > 0 and (dl - _last["bytes"]) >= total * 0.05:
                _last["bytes"] = dl
                loop.call_soon_threadsafe(
                    asyncio.ensure_future,
                    progress_callback(dl, total),
                )

    ydl_opts = {
        **_BASE_OPTS,
        "format":         fmt,
        "outtmpl":        outtmpl,
        "max_filesize":   MAX_FILE_SIZE_MB * 1024 * 1024,
        "progress_hooks": [_progress_hook],
    }

    if is_audio:
        ydl_opts.update({
            "format":         "bestaudio/best",
            "writethumbnail": True,
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "320"},
                {"key": "FFmpegMetadata",     "add_metadata": True},
                {"key": "EmbedThumbnail"},
            ],
        })

    def _download():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info     = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            if is_audio:
                filename = os.path.splitext(filename)[0] + ".mp3"
            return {
                "path":          filename,
                "title":         safe_filename(info.get("title") or info.get("id", "video")),
                "is_audio":      is_audio,
                "duration":      int(float(info.get("duration") or 0)),
                "uploader":      info.get("uploader") or info.get("channel") or "",
                "thumbnail_url": _best_thumbnail(info),
            }

    try:
        result = await loop.run_in_executor(_EXECUTOR, _download)
        path   = result["path"]
        if not os.path.exists(path):
            base = os.path.splitext(path)[0]
            for ext in (".mp4", ".mkv", ".webm", ".mp3", ".m4a", ".ogg"):
                if os.path.exists(base + ext):
                    result["path"] = base + ext
                    break
            else:
                logger.error(f"Downloaded file not found: {path}")
                return None
        result["size_mb"] = _size_mb(result["path"])
        return result

    except yt_dlp.utils.DownloadError as e:
        msg = str(e).lower()
        if "sign in" in msg or ("confirm" in msg and "bot" in msg):
            raise ValueError(
                "⚠️ YouTube is blocking this server.\n"
                "The bgutil PO-token server may still be warming up.\n"
                "Please wait 30 seconds and try again."
            )
        if "private"       in msg: raise ValueError("This content is private or requires login.")
        if "geo"           in msg: raise ValueError("This content is not available in your region.")
        if "not available" in msg: raise ValueError("This content is not available.")
        if "age"           in msg: raise ValueError("This content is age-restricted.")
        if "live"          in msg: raise ValueError("Live streams cannot be downloaded.")
        if "copyright"     in msg: raise ValueError("Blocked due to copyright.")
        if "too large"     in msg or "filesize" in msg:
            raise ValueError(f"File exceeds the {MAX_FILE_SIZE_MB} MB limit.")
        if "unsupported"   in msg: raise ValueError("This URL is not supported.")
        raise ValueError(f"Download failed: {e}")
    except Exception as e:
        logger.error(f"Unexpected download error: {e}")
        raise


# ── yt-dlp metadata ───────────────────────────────────────────────────────────

async def _fetch_ydlp_meta(url: str) -> Optional[dict]:
    def _extract():
        with yt_dlp.YoutubeDL(_FAST_META_OPTS) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        loop = asyncio.get_running_loop()
        info = await asyncio.wait_for(
            loop.run_in_executor(_EXECUTOR, _extract),
            timeout=10,
        )
        if not info:
            return None
        return {
            "title":       safe_filename(info.get("title") or info.get("id", "video")),
            "duration":    int(float(info.get("duration") or 0)),
            "uploader":    info.get("uploader") or info.get("channel") or "Unknown",
            "thumbnail":   _best_thumbnail(info),
            "is_live":     bool(info.get("is_live")),
            "resolutions": _DEFAULT_RESOLUTIONS,
            "platform":    info.get("extractor_key", "Unknown"),
        }
    except asyncio.TimeoutError:
        logger.warning(f"Metadata timeout for {url[:60]}")
        return None
    except yt_dlp.utils.DownloadError as e:
        logger.warning(f"Metadata fetch failed: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected metadata error: {e}")
        return None


# ── Public: fetch_metadata ────────────────────────────────────────────────────

async def fetch_metadata(url: str) -> Optional[dict]:
    if not _is_supported(url):
        return None

    cached = _cache_get(url)
    if cached:
        return cached

    t0 = time.monotonic()

    if _is_youtube(url):
        meta = await _fetch_youtube_oembed(url)
        if meta:
            _cache_set(url, meta)
            logger.info(f"YouTube oEmbed {(time.monotonic()-t0)*1000:.0f}ms")
            return meta

    elif _is_spotify_track(url):
        meta = await _fetch_spotify_meta(url)
        if meta:
            _cache_set(url, meta)
        return meta

    elif _is_apple_music_song(url):
        meta = await _fetch_apple_music_meta(url)
        if meta:
            _cache_set(url, meta)
        return meta

    elif _is_instagram(url):
        meta = await _fetch_ydlp_meta(url)
        if meta:
            meta["platform"] = "Instagram"
            _cache_set(url, meta)
        return meta

    meta = await _fetch_ydlp_meta(url)
    if meta:
        _cache_set(url, meta)
    return meta


# ── Apple Music helpers ───────────────────────────────────────────────────────

def _apple_entry(i, track_id, track_name, artist, duration, art, track_url):
    return {
        "index":     i + 1,
        "id":        track_id,
        "title":     track_name,
        "uploader":  artist,
        "url":       track_url,
        "duration":  duration,
        "thumbnail": art,
        "_prefill": {
            "title":         track_name,
            "duration":      duration,
            "uploader":      artist,
            "thumbnail":     art,
            "is_live":       False,
            "resolutions":   ["audio"],
            "platform":      "Apple Music",
            "_apple_title":  track_name,
            "_apple_artist": artist,
            "_apple_album":  "",
        },
    }


async def fetch_apple_music_album_metadata(url: str) -> Optional[dict]:
    album_id = _apple_album_id(url)
    if not album_id:
        return None
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as http:
            r = await http.get(f"https://itunes.apple.com/lookup?id={album_id}&entity=song")
            if r.status_code != 200:
                return None
            data = r.json()
    except Exception as e:
        logger.warning(f"iTunes album lookup failed: {e}")
        return None

    results = data.get("results", [])
    if not results:
        return None

    album_info  = results[0]
    album_title = safe_filename(album_info.get("collectionName") or "Album")
    artist_name = album_info.get("artistName") or "Unknown"
    thumb       = album_info.get("artworkUrl100", "").replace("100x100bb", "600x600bb")

    entries = []
    for i, track in enumerate(results[1:]):
        if track.get("wrapperType") != "track":
            continue
        track_name = safe_filename(track.get("trackName") or f"Track {i+1}")
        track_id   = str(track.get("trackId") or "")
        dur_ms     = int(track.get("trackTimeMillis") or 0)
        art        = track.get("artworkUrl100", "").replace("100x100bb", "300x300bb")
        track_url  = (track.get("trackViewUrl") or f"https://music.apple.com/song/{track_id}").split("?")[0]
        if not track_id:
            continue
        entries.append(_apple_entry(i, track_id, track_name, artist_name, dur_ms // 1000, art, track_url))

    if not entries:
        return None
    for i, e in enumerate(entries):
        e["index"] = i + 1

    return {
        "playlist_title": album_title,
        "uploader":       artist_name,
        "total":          len(entries),
        "entries":        entries,
        "platform":       "Apple Music",
        "resolutions":    _AUDIO_ONLY,
        "thumbnail":      thumb,
    }


async def fetch_apple_music_playlist_metadata(url: str) -> Optional[dict]:
    """3-strategy Apple Music playlist scraper."""
    playlist_id = _apple_playlist_id(url)
    if not playlist_id:
        return None

    country = "us"
    m = re.search(r"music\.apple\.com/([a-z]{2})/", url.lower())
    if m:
        country = m.group(1)

    clean_url = f"https://music.apple.com/{country}/playlist/{playlist_id}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
    }
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers=headers) as http:
            r = await http.get(clean_url)
            if r.status_code != 200:
                return None
            html = r.text
    except Exception as e:
        logger.warning(f"Apple Music playlist fetch failed: {e}")
        return None

    pl_title = thumbnail = ""
    entries  = []

    # Strategy 1: serialized-server-data
    m = re.search(r'<script id="serialized-server-data"[^>]*>(\[.+?\])</script>', html, re.DOTALL)
    if m:
        try:
            ssd = json.loads(m.group(1))

            def _walk(obj, depth=0):
                if depth > 20 or obj is None:
                    return None, [], ""
                if isinstance(obj, dict):
                    if (isinstance(obj.get("attributes"), dict)
                            and obj["attributes"].get("name")
                            and obj.get("relationships", {}).get("tracks")):
                        attrs      = obj["attributes"]
                        track_data = obj["relationships"]["tracks"].get("data") or []
                        art_obj    = attrs.get("artwork", {})
                        art_url    = art_obj.get("url", "")
                        if art_url:
                            art_url = art_url.replace("{w}", "600").replace("{h}", "600").replace("{f}", "jpg")
                        if attrs.get("name") and track_data:
                            return attrs["name"], track_data, art_url
                    for v in obj.values():
                        r2 = _walk(v, depth + 1)
                        if r2 and r2[0]:
                            return r2
                elif isinstance(obj, list):
                    for item in obj:
                        r2 = _walk(item, depth + 1)
                        if r2 and r2[0]:
                            return r2
                return None, [], ""

            result = _walk(ssd)
            if result and result[0]:
                pl_title   = safe_filename(result[0])
                thumbnail  = result[2]
                for i, t in enumerate(result[1]):
                    attrs      = t.get("attributes", {})
                    track_name = safe_filename(attrs.get("name") or f"Track {i+1}")
                    artist     = attrs.get("artistName") or ""
                    dur_ms     = int(attrs.get("durationInMillis") or 0)
                    art_obj    = attrs.get("artwork", {})
                    art_url    = art_obj.get("url", "")
                    if art_url:
                        art_url = art_url.replace("{w}", "300").replace("{h}", "300").replace("{f}", "jpg")
                    track_id  = t.get("id", "")
                    track_url = f"https://music.apple.com/song/{track_id}" if track_id else ""
                    if track_url:
                        entries.append(_apple_entry(i, track_id, track_name, artist, dur_ms // 1000, art_url, track_url))
            logger.info(f"Apple Music playlist strategy 1: {len(entries)} tracks")
        except Exception as e:
            logger.debug(f"Strategy 1 failed: {e}")

    # Strategy 2: JSON-LD
    if not entries:
        for ld_raw in re.findall(r'<script type="application/ld\+json"[^>]*>(.+?)</script>', html, re.DOTALL):
            try:
                ld = json.loads(ld_raw)
                if not isinstance(ld, dict):
                    continue
                if "MusicPlaylist" not in ld.get("@type", ""):
                    continue
                pl_title = safe_filename(ld.get("name") or "Playlist")
                for i, t in enumerate(ld.get("track") or []):
                    track_name = safe_filename(t.get("name") or f"Track {i+1}")
                    by_artist  = t.get("byArtist")
                    artist     = by_artist.get("name", "") if isinstance(by_artist, dict) else ""
                    track_url  = (t.get("url") or "").split("?")[0]
                    if not track_url:
                        continue
                    tid_m = re.search(r"/(\d+)$", track_url)
                    tid   = tid_m.group(1) if tid_m else ""
                    entries.append(_apple_entry(i, tid, track_name, artist, 0, "", track_url))
                if entries:
                    break
            except Exception:
                continue
        logger.info(f"Apple Music playlist strategy 2: {len(entries)} tracks")

    # Strategy 3: regex
    if not entries:
        seen = set()
        for song_url in re.findall(r'https://music\.apple\.com/[a-z]{2}/song/[^"\'>\s]+', html):
            clean = song_url.split("?")[0]
            if clean in seen:
                continue
            seen.add(clean)
            tid_m  = re.search(r"/(\d+)$", clean)
            tid    = tid_m.group(1) if tid_m else ""
            slug_m = re.search(r"/song/([^/]+)/\d+", clean)
            slug   = slug_m.group(1).replace("-", " ").title() if slug_m else f"Track {len(entries)+1}"
            entries.append(_apple_entry(len(entries), tid, safe_filename(slug), "", 0, "", clean))
        logger.info(f"Apple Music playlist strategy 3: {len(entries)} tracks")

    if not pl_title:
        m2 = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', html)
        if m2:
            pl_title = safe_filename(m2.group(1))
    if not thumbnail:
        m3 = re.search(r'<meta\s+property="og:image"\s+content="([^"]+)"', html)
        if m3:
            thumbnail = m3.group(1)

    if not entries:
        return None

    for i, e in enumerate(entries):
        e["index"] = i + 1

    return {
        "playlist_title": pl_title or "Apple Music Playlist",
        "uploader":       "Apple Music",
        "total":          len(entries),
        "entries":        entries,
        "platform":       "Apple Music",
        "resolutions":    _AUDIO_ONLY,
        "thumbnail":      thumbnail,
    }


# ── Public: fetch_playlist_metadata ──────────────────────────────────────────

async def fetch_playlist_metadata(url: str) -> Optional[dict]:
    if _is_spotify_playlist(url):
        return await fetch_spotify_playlist_metadata(url)
    if _is_spotify_album(url):
        return await fetch_spotify_album_metadata(url)
    if _is_apple_music_playlist(url):
        return await fetch_apple_music_playlist_metadata(url)
    if _is_apple_music_album(url):
        return await fetch_apple_music_album_metadata(url)
    if not _is_youtube(url):
        return None

    ydl_opts = {
        "quiet":          True,
        "no_warnings":    True,
        "skip_download":  True,
        "extract_flat":   True,
        "socket_timeout": 10,
        "http_headers":   {"User-Agent": _BROWSER_UA},
    }

    def _extract():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        loop = asyncio.get_running_loop()
        info = await asyncio.wait_for(loop.run_in_executor(_EXECUTOR, _extract), timeout=15)
        if not info:
            return None
        entries_raw = info.get("entries") or []
        if not entries_raw:
            return None
        entries = []
        for i, e in enumerate(entries_raw):
            if not e:
                continue
            vid_url = e.get("url") or e.get("webpage_url") or ""
            vid_id  = e.get("id") or ""
            if not vid_url and vid_id:
                vid_url = f"https://www.youtube.com/watch?v={vid_id}"
            entries.append({
                "index":     i + 1,
                "id":        vid_id,
                "title":     safe_filename(e.get("title") or vid_id or f"Video {i+1}"),
                "url":       vid_url,
                "duration":  int(float(e.get("duration") or 0)),
                "thumbnail": (e.get("thumbnails") or [{}])[0].get("url", "") or e.get("thumbnail", ""),
            })
        return {
            "playlist_title": safe_filename(info.get("title") or "Playlist"),
            "uploader":       info.get("uploader") or info.get("channel") or "Unknown",
            "total":          len(entries),
            "entries":        entries,
            "platform":       "YouTube",
            "resolutions":    _DEFAULT_RESOLUTIONS,
        }
    except asyncio.TimeoutError:
        logger.warning(f"Playlist timeout for {url[:60]}")
        return None
    except Exception as e:
        logger.error(f"Playlist metadata error: {e}")
        return None


# ── Public: download_media ────────────────────────────────────────────────────

async def download_media(
    url: str,
    user_id: int,
    quality: str = "best",
    progress_callback: Optional[Callable] = None,
) -> Optional[dict]:
    if not _is_supported(url):
        from config import UNSUPPORTED_TXT
        raise ValueError(UNSUPPORTED_TXT)

    if _is_spotify(url):
        return await _download_spotify(url, user_id, progress_callback)

    if _is_apple_music(url):
        return await _download_apple_music(url, user_id, progress_callback)

    if _is_instagram(url):
        return await _download_instagram(url, user_id, progress_callback)

    return await _download_ytdlp(url, user_id, quality, progress_callback)

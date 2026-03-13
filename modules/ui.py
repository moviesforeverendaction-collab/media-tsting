"""
modules/ui.py
UI helpers: progress bars, keyboards, wallpaper, reactions.
"""

import asyncio
import logging
import os
import random
import time
from io import BytesIO
from typing import Optional

import httpx
from pyrogram import Client
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import (
    REACTIONS, STATIC_WALLPAPER,
    CE_TICK, CE_CROSS, CE_CLOCK, CE_FIRE, CE_LIGHTNING, CE_DL,
    CE_MUSIC, CE_CHART, CE_SPARKLE, CE_GEAR, CE_HOME, CE_TRASH,
    CE_PLAY, CE_REFRESH, CE_WARN, CE_STOP, CE_QUESTION,
)

logger = logging.getLogger(__name__)

# ── Quality badge mapping ─────────────────────────────────────────────────────
QUALITY_BADGES = {
    "4320p": "🔵 8K",
    "2160p": "🔷 4K Ultra",
    "1440p": "🟣 2K",
    "1080p": "🟢 1080p HD",
    "720p":  "🟡 720p",
    "480p":  "🟠 480p",
    "360p":  "🔴 360p",
    "240p":  "⚫ 240p",
    "best":  "⭐ Best",
    "audio": "🎵 Audio MP3",
}


def _quality_label(res: str) -> str:
    return QUALITY_BADGES.get(res, f"📹 {res}")


# ── Progress bar ──────────────────────────────────────────────────────────────

def progress_bar(current: int, total: int, width: int = 16) -> str:
    """
    Smooth 8-step Unicode block progress bar.
    Example:  ████████▌░░░░░░░  53%
    """
    if total <= 0:
        return "░" * width

    ratio  = min(current / total, 1.0)
    filled = ratio * width
    full   = int(filled)
    frac   = filled - full

    sub_chars = " ▏▎▍▌▋▊▉█"
    sub = sub_chars[int(frac * 8)]

    bar  = "█" * full + sub + "░" * max(width - full - 1, 0)
    return bar[:width]


def _fmt_size(b: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"


def _fmt_dur(s: int) -> str:
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def format_progress(current: int, total: int, speed: float = 0, elapsed: float = 0) -> str:
    """Full progress block: bar, %, size, speed, ETA."""
    bar = progress_bar(current, total)
    pct = (current / total * 100) if total else 0
    cur = _fmt_size(current)
    tot = _fmt_size(total)

    lines = [
        f"<code>[{bar}]</code>  <b>{pct:.1f}%</b>",
        f"<b>{cur}</b> / <b>{tot}</b>",
    ]

    if speed > 0:
        lines.append(f"{CE_FIRE} <b>{_fmt_size(speed)}/s</b>")
        if total > current:
            eta = int((total - current) / speed)
            lines.append(f"{CE_CLOCK} ETA <b>{_fmt_dur(eta)}</b>")
    elif elapsed > 0:
        lines.append(f"{CE_CLOCK} <b>{_fmt_dur(int(elapsed))}</b> elapsed")

    return "\n".join(lines)


# ── Downloading status message ────────────────────────────────────────────────

def download_start_text(url: str, quality: str, platform: str) -> str:
    """Initial status message shown when a download begins."""
    q_label  = _quality_label(quality)
    icon     = CE_MUSIC if quality == "audio" or platform in ("Spotify", "Apple Music") else CE_DL
    p_icon   = {
        "YouTube":     "▶️",
        "Spotify":     "🎧",
        "Apple Music": "🍎",
        "Instagram":   "📸",
    }.get(platform, "🌐")
    return (
        f"{icon} <b>Starting download...</b>\n\n"
        f"<blockquote>"
        f"{p_icon} <b>{platform}</b>  ·  <b>{q_label}</b>\n"
        f"<code>{url[:72]}{'…' if len(url) > 72 else ''}</code>"
        f"</blockquote>"
    )


def downloading_text(url: str, progress_block: str) -> str:
    """Live downloading message body."""
    return (
        f"{CE_DL} <b>Downloading...</b>\n\n"
        f"{progress_block}"
    )


def uploading_text(title: str, part_label: str = "") -> str:
    """Status shown while uploading to Telegram."""
    suffix = f"  <code>{part_label}</code>" if part_label else ""
    return f"📤 <b>Uploading{suffix}...</b>\n<b>{title[:60]}</b>"


# ── Wallpaper ─────────────────────────────────────────────────────────────────

async def fetch_wallpaper() -> Optional[BytesIO]:
    sources = [
        "https://picsum.photos/800/1000?random&blur=1",
        "https://loremflickr.com/800/1000/abstract,dark,technology",
        STATIC_WALLPAPER,
    ]
    async with httpx.AsyncClient(timeout=8, follow_redirects=True) as http:
        for url in sources:
            try:
                r   = await http.get(url)
                r.raise_for_status()
                bio = BytesIO(r.content)
                bio.name = "wallpaper.jpg"
                return bio
            except Exception as e:
                logger.debug(f"Wallpaper source failed ({url[:50]}): {e}")
    return None


# ── Reactions ─────────────────────────────────────────────────────────────────

async def send_reaction(client: Client, chat_id: int, message_id: int):
    try:
        emoji = random.choice(REACTIONS)
        await client.send_reaction(chat_id=chat_id, message_id=message_id, emoji=emoji)
    except Exception:
        pass


# ── Keyboards ─────────────────────────────────────────────────────────────────

def start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📖 Help",       callback_data="ui:help"),
            InlineKeyboardButton("ℹ️ About",      callback_data="ui:about"),
        ],
        [
            InlineKeyboardButton("📊 Stats",      callback_data="ui:stats"),
            InlineKeyboardButton("🔗 Share Bot",  switch_inline_query=""),
        ],
    ])


def back_keyboard(target: str = "start") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"🏠 Main Menu", callback_data=f"ui:{target}"),
    ]])


def quality_keyboard(
    resolutions: list,
    user_id: int,
    uid: str,
    is_playlist: bool = False,
) -> InlineKeyboardMarkup:
    """
    Quality selector.
    Layout: 2 columns of quality buttons, then audio row, then cancel.
    Playlist sessions show no thumbnail button.
    """
    prefix  = "plq" if is_playlist else "dl"
    buttons = []
    row     = []

    for res in resolutions:
        row.append(InlineKeyboardButton(
            _quality_label(res),
            callback_data=f"{prefix}:{user_id}:{uid}:{res}",
        ))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    if is_playlist:
        buttons.append([
            InlineKeyboardButton("🎵 MP3 Audio", callback_data=f"{prefix}:{user_id}:{uid}:audio"),
        ])
    else:
        buttons.append([
            InlineKeyboardButton("🎵 MP3 Audio",   callback_data=f"{prefix}:{user_id}:{uid}:audio"),
            InlineKeyboardButton("🖼 Thumbnail",    callback_data=f"thumb:{user_id}:{uid}"),
        ])

    buttons.append([
        InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{user_id}"),
    ])

    return InlineKeyboardMarkup(buttons)


def playlist_keyboard(
    pl: dict,
    user_id: int,
    pid: str,
    quality: str,
    page: int,
    page_size: int = 8,
) -> tuple[str, InlineKeyboardMarkup]:
    """Paginated playlist track browser. Returns (caption, keyboard)."""
    from modules.util import format_duration

    entries     = pl["entries"]
    total       = pl["total"]
    start       = page * page_size
    end         = min(start + page_size, total)
    page_items  = entries[start:end]
    total_pages = max(1, (total + page_size - 1) // page_size)
    badge       = _quality_label(quality)
    platform    = pl.get("platform", "")
    is_music    = platform in ("Spotify", "Apple Music")
    item_label  = "tracks" if is_music else "videos"
    icon        = "🎵" if is_music else "📋"

    text = (
        f"<b>{icon} {pl['playlist_title']}</b>\n"
        f"<b>👤 {pl['uploader']}</b>  ·  <b>{total} {item_label}</b>  ·  {badge}\n"
        f"<i>Page {page + 1}/{total_pages} — tap a {item_label[:-1]} to download</i>"
    )

    buttons = []
    for e in page_items:
        dur   = f"  [{format_duration(e['duration'])}]" if e.get("duration") else ""
        title = e["title"][:36]
        label = f"▶ {e['index']}. {title}{dur}"
        buttons.append([InlineKeyboardButton(
            label,
            callback_data=f"pli:{user_id}:{pid}:{quality}:{e['index'] - 1}",
        )])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"plp:{user_id}:{pid}:{quality}:{page - 1}"))
    if end < total:
        nav.append(InlineKeyboardButton("Next ▶", callback_data=f"plp:{user_id}:{pid}:{quality}:{page + 1}"))
    if nav:
        buttons.append(nav)

    from config import MAX_PLAYLIST_ALL
    all_label = (
        f"⬇️ Download All ({total})" if total <= MAX_PLAYLIST_ALL
        else f"⬇️ Download First {MAX_PLAYLIST_ALL}"
    )
    buttons.append([
        InlineKeyboardButton(all_label,    callback_data=f"pla:{user_id}:{pid}:{quality}"),
        InlineKeyboardButton("❌ Cancel",   callback_data=f"cancel:{user_id}"),
    ])

    return text, InlineKeyboardMarkup(buttons)
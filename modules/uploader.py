"""
modules/uploader.py
Uploads downloaded media to Telegram with clean progress UI.
Handles single files, split parts, and Instagram media albums.
"""

import os
import time
import logging
from typing import Optional

from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.types import Message

from modules.util import (
    extract_thumbnail, get_video_duration, delete_files, format_duration,
    cleanup_dir,
)
from modules.ui import format_progress, _fmt_size
from modules.splitter import needs_splitting, split_file

logger = logging.getLogger(__name__)

_UPLOAD_INTERVAL = 3   # seconds between progress edits


async def upload_media(
    client: Client,
    msg: Message,
    status_msg: Message,
    download_result: dict,
) -> bool:
    path     = download_result["path"]
    title    = download_result.get("title", "media")
    is_audio = download_result.get("is_audio", False)
    duration = download_result.get("duration", 0)
    uploader = download_result.get("uploader", "")
    size_mb  = download_result.get("size_mb", 0)
    user_id  = (
        (msg.from_user.id if msg and msg.from_user else None)
        or download_result.get("user_id", 0)
    )
    all_media = download_result.get("_all_media")

    # Instagram album (multiple photos/videos)
    if all_media and len(all_media) > 1:
        return await _upload_album(client, msg, status_msg, all_media, title, user_id)

    # Large file — split into parts
    if needs_splitting(path):
        return await _upload_split(
            client, msg, status_msg, path, title, is_audio, duration, uploader, size_mb, user_id
        )

    return await _upload_single(
        client, msg, status_msg, path, title, is_audio, duration, uploader, size_mb, user_id
    )


# ── Instagram album ───────────────────────────────────────────────────────────

async def _upload_album(
    client: Client,
    msg: Message,
    status_msg: Message,
    media_list: list,
    title: str,
    user_id: int,
) -> bool:
    total = len(media_list)
    await status_msg.edit_text(
        f"📤 <b>Uploading {total} files...</b>\n<b>{title[:60]}</b>",
        parse_mode=ParseMode.HTML,
    )

    sent = 0
    for i, item in enumerate(media_list, 1):
        path  = item["path"]
        mtype = item.get("type", "photo")
        cap   = f"<b>{title[:60]}</b>  ({i}/{total})\n📥 @MediaFetchBot"

        try:
            await status_msg.edit_text(
                f"📤 <b>Uploading {i}/{total}...</b>\n<b>{title[:60]}</b>",
                parse_mode=ParseMode.HTML,
            )
            if mtype == "photo":
                await client.send_photo(
                    chat_id=msg.chat.id, photo=path,
                    caption=cap, parse_mode=ParseMode.HTML,
                )
            else:
                thumb = await extract_thumbnail(path, user_id)
                dur   = item.get("duration") or await get_video_duration(path)
                await client.send_video(
                    chat_id=msg.chat.id, video=path, caption=cap,
                    parse_mode=ParseMode.HTML, duration=dur,
                    thumb=thumb, supports_streaming=True,
                )
                if thumb:
                    delete_files(thumb)
            sent += 1
        except Exception as e:
            logger.error(f"Album item {i} upload failed: {e}")
        finally:
            delete_files(path)

    try:
        if sent == total:
            await status_msg.edit_text(
                f"✅ <b>All {total} files uploaded!</b>",
                parse_mode=ParseMode.HTML,
            )
        else:
            await status_msg.edit_text(
                f"⚠️ <b>{sent}/{total} files uploaded.</b> Some failed.",
                parse_mode=ParseMode.HTML,
            )
    except Exception:
        pass

    return sent == total


# ── Split upload ──────────────────────────────────────────────────────────────

async def _upload_split(
    client, msg, status_msg, path, title, is_audio, duration, uploader, size_mb, user_id
) -> bool:
    await status_msg.edit_text(
        f"✂️ <b>Splitting {size_mb:.0f} MB into parts...</b>",
        parse_mode=ParseMode.HTML,
    )
    parts = await split_file(path, title, user_id)
    if not parts:
        await status_msg.edit_text("❌ <b>Failed to split file.</b>", parse_mode=ParseMode.HTML)
        delete_files(path)
        return False

    total  = parts[0]["total"]
    all_ok = True
    await status_msg.edit_text(
        f"✂️ <b>Split into {total} parts.</b> Uploading...",
        parse_mode=ParseMode.HTML,
    )
    for part in parts:
        part_size = os.path.getsize(part["path"]) / (1024 * 1024) if os.path.exists(part["path"]) else 0
        ok = await _upload_single(
            client, msg, status_msg,
            part["path"], title, is_audio, 0, uploader, part_size, user_id,
            part_label=part["label"], delete_after=True,
        )
        if not ok:
            all_ok = False

    delete_files(path)
    try:
        if all_ok:
            await status_msg.edit_text(
                f"✅ <b>All {total} parts uploaded!</b>", parse_mode=ParseMode.HTML
            )
        else:
            await status_msg.edit_text(
                f"⚠️ <b>Some parts failed to upload.</b>", parse_mode=ParseMode.HTML
            )
    except Exception:
        pass
    return all_ok


# ── Single file upload ────────────────────────────────────────────────────────

async def _upload_single(
    client, msg, status_msg, path, title, is_audio, duration, uploader, size_mb, user_id,
    part_label: Optional[str] = None,
    delete_after: bool = True,
) -> bool:
    thumb_path = None
    try:
        # Build caption
        lines = [f"<b>{title[:60]}</b>"]
        if part_label:
            lines.append(f"📦 <b>{part_label}</b>")
        if uploader:
            lines.append(f"👤 {uploader}")
        if duration:
            lines.append(f"⏱ {format_duration(duration)}")
        lines.append(f"💾 {size_mb:.1f} MB")
        lines.append("📥 @MediaFetchBot")
        caption = "\n".join(lines)

        # Progress tracker
        _last  = [time.monotonic()]
        _start = [time.monotonic()]
        _prev  = [0]

        async def upload_progress(current: int, total: int):
            now = time.monotonic()
            if now - _last[0] < _UPLOAD_INTERVAL:
                return
            speed    = (current - _prev[0]) / max(now - _last[0], 0.001)
            elapsed  = now - _start[0]
            _last[0] = now
            _prev[0] = current
            suffix   = f"  <code>{part_label}</code>" if part_label else ""
            try:
                await status_msg.edit_text(
                    f"📤 <b>Uploading{suffix}...</b>\n"
                    f"<b>{title[:50]}</b>\n\n"
                    + format_progress(current, total, speed, elapsed),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

        # Initial upload status
        suffix = f"  <code>{part_label}</code>" if part_label else ""
        await status_msg.edit_text(
            f"📤 <b>Uploading{suffix}...</b>\n<b>{title[:60]}</b>",
            parse_mode=ParseMode.HTML,
        )

        if is_audio:
            await client.send_audio(
                chat_id=msg.chat.id,
                audio=path,
                title=title,
                performer=uploader,
                duration=duration,
                caption=caption,
                parse_mode=ParseMode.HTML,
                progress=upload_progress,
            )
        else:
            thumb_path = await extract_thumbnail(path, user_id)
            if not duration:
                duration = await get_video_duration(path)

            await client.send_video(
                chat_id=msg.chat.id,
                video=path,
                caption=caption,
                parse_mode=ParseMode.HTML,
                duration=duration,
                thumb=thumb_path,
                supports_streaming=True,
                progress=upload_progress,
            )

        return True

    except Exception as e:
        logger.error(f"Upload failed [{part_label or 'single'}] user={user_id}: {e}")
        try:
            suffix = f" for {part_label}" if part_label else ""
            await status_msg.edit_text(
                f"❌ <b>Upload failed{suffix}.</b>\n<code>{str(e)[:200]}</code>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        return False

    finally:
        if delete_after:
            delete_files(path, thumb_path)
        elif thumb_path:
            delete_files(thumb_path)
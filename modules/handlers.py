"""
modules/handlers.py
All Telegram message & callback handlers.
"""

import logging
import uuid
import time
from io import BytesIO

import httpx
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import Message, CallbackQuery

from config import (
    START_TXT, HELP_TXT, ABOUT_TXT, UNSUPPORTED_TXT,
    CE_TICK, CE_CROSS, CE_CLOCK, CE_DL, CE_CHART, CE_FIRE,
    CE_SEARCH, CE_GEAR, CE_WARN, CE_STOP, CE_BELL,
    MAX_PLAYLIST_ALL,
)
from database.mongo import (
    check_rate_limit, push_job, queue_length, get_stats,
    register_user, cancel_user_job, is_user_processing,
    store_url, load_url, store_json, load_json,
)
from modules.util import extract_urls, format_duration
from modules.download import (
    fetch_metadata, fetch_playlist_metadata,
    _is_youtube, _is_spotify, _is_apple_music, _is_instagram, _is_supported,
    _cache_set,
)
from modules.ui import (
    fetch_wallpaper, send_reaction, start_keyboard, back_keyboard,
    quality_keyboard, playlist_keyboard,
)

logger = logging.getLogger(__name__)

_PAGE_SIZE = 8


def _is_playlist(url: str) -> bool:
    u = url.lower()
    return (
        ("list=" in u and ("youtube.com" in u or "youtu.be" in u))
        or "/playlist/" in u
        or "open.spotify.com/album/" in u
        or ("music.apple.com/" in u and "/album/" in u)
    )


def register_handlers(app: Client):

    # ── /start ────────────────────────────────────────────────────────────────

    @app.on_message(filters.command("start"))
    async def cmd_start(client: Client, msg: Message):
        await register_user(msg.from_user.id)
        me   = await client.get_me()
        text = START_TXT(
            first_name   = msg.from_user.first_name or "there",
            bot_username = me.username,
            bot_name     = me.first_name,
        )
        photo = await fetch_wallpaper()
        try:
            if photo:
                await msg.reply_photo(
                    photo=photo, caption=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=start_keyboard(),
                )
            else:
                await msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=start_keyboard())
        except Exception:
            await msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=start_keyboard())
        await send_reaction(client, msg.chat.id, msg.id)

    # ── /help ─────────────────────────────────────────────────────────────────

    @app.on_message(filters.command("help"))
    async def cmd_help(client: Client, msg: Message):
        await msg.reply_text(
            HELP_TXT, parse_mode=ParseMode.HTML,
            reply_markup=back_keyboard(), disable_web_page_preview=True,
        )

    # ── /about ────────────────────────────────────────────────────────────────

    @app.on_message(filters.command("about"))
    async def cmd_about(client: Client, msg: Message):
        me = await client.get_me()
        await msg.reply_text(
            ABOUT_TXT(me.username), parse_mode=ParseMode.HTML,
            reply_markup=back_keyboard(), disable_web_page_preview=True,
        )

    # ── /stats ────────────────────────────────────────────────────────────────

    @app.on_message(filters.command("stats"))
    async def cmd_stats(client: Client, msg: Message):
        await _send_stats(client, msg.chat.id, reply_to=msg.id)

    async def _send_stats(client, chat_id, reply_to=None, edit_msg=None):
        stats = await get_stats()
        q_len = await queue_length()
        text  = (
            f"<b>{CE_CHART} Bot Statistics</b>\n\n"
            "<blockquote>"
            f"<b>👤 Users:</b>       <code>{stats['total_users']:,}</code>\n"
            f"<b>{CE_TICK} Downloads:</b>  <code>{stats['total_downloads']:,}</code>\n"
            f"<b>{CE_CROSS} Errors:</b>     <code>{stats['total_errors']:,}</code>\n"
            f"<b>💾 Data sent:</b>   <code>{stats['total_bytes'] / (1024**3):.2f} GB</code>\n"
            f"<b>📋 Queue:</b>       <code>{q_len}</code>"
            "</blockquote>"
        )
        if edit_msg:
            await edit_msg.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=back_keyboard())
        else:
            await client.send_message(
                chat_id, text, parse_mode=ParseMode.HTML,
                reply_markup=back_keyboard(), reply_to_message_id=reply_to,
            )

    # ── /cancel ───────────────────────────────────────────────────────────────

    @app.on_message(filters.command("cancel"))
    async def cmd_cancel(client: Client, msg: Message):
        if await is_user_processing(msg.from_user.id):
            await cancel_user_job(msg.from_user.id)
            await msg.reply_text(
                f"{CE_STOP} <b>Cancellation requested.</b> Stopping shortly.",
                parse_mode=ParseMode.HTML,
            )
        else:
            await msg.reply_text(f"{CE_WARN} No active download to cancel.")

    # ── /ping ─────────────────────────────────────────────────────────────────

    @app.on_message(filters.command("ping"))
    async def cmd_ping(client: Client, msg: Message):
        t0   = time.monotonic()
        sent = await msg.reply_text("🏓 Pong!")
        ms   = (time.monotonic() - t0) * 1000
        await sent.edit_text(f"🏓 <b>Pong!</b>  <code>{ms:.0f}ms</code>", parse_mode=ParseMode.HTML)

    # ── /request ──────────────────────────────────────────────────────────────

    @app.on_message(filters.command("request"))
    async def cmd_request(client: Client, msg: Message):
        await msg.reply_text(
            f"{CE_BELL} <b>Feature Request</b>\n\n"
            "Have a platform you want us to support?\n"
            "Tell us what you'd like added — just reply or send a message with the platform name "
            "and a sample link.\n\n"
            "<i>We review all requests and add popular ones in updates!</i>",
            parse_mode=ParseMode.HTML,
        )

    # ── URL / text handler ────────────────────────────────────────────────────

    @app.on_message(
        filters.text & filters.private
        & ~filters.command(["start", "help", "about", "stats", "cancel", "ping", "request"])
    )
    async def handle_url(client: Client, msg: Message):
        user_id = msg.from_user.id
        urls    = extract_urls(msg.text.strip())

        if not urls:
            await msg.reply_text(
                f"{CE_SEARCH} <b>Send me a supported link!</b>\n\n"
                "Supported: YouTube · Spotify · Apple Music · Instagram\n"
                "Use /help for the full guide.",
                parse_mode=ParseMode.HTML,
            )
            return

        url = urls[0]

        # ── Unsupported platform ───────────────────────────────────────────
        if not _is_supported(url):
            await msg.reply_text(UNSUPPORTED_TXT, parse_mode=ParseMode.HTML)
            return

        # ── Rate limit ─────────────────────────────────────────────────────
        allowed, retry_after = await check_rate_limit(user_id)
        if not allowed:
            await msg.reply_text(
                f"{CE_WARN} <b>Rate limited.</b> Try again in <b>{retry_after}s</b>.",
                parse_mode=ParseMode.HTML,
            )
            return

        is_pl  = _is_playlist(url)
        status = await msg.reply_text(
            f"{CE_SEARCH} <b>Fetching {'playlist' if is_pl else 'info'}...</b>",
            parse_mode=ParseMode.HTML,
        )

        if is_pl:
            await _handle_playlist(client, msg, status, url, user_id)
        else:
            await _handle_single(client, msg, status, url, user_id)

        await send_reaction(client, msg.chat.id, msg.id)

    # ── Single item ───────────────────────────────────────────────────────────

    async def _handle_single(client, msg, status, url, user_id):
        meta = await fetch_metadata(url)
        if not meta:
            await status.edit_text(
                f"{CE_CROSS} <b>Could not fetch info.</b>\n"
                "The link may be invalid, private, or unavailable.",
                parse_mode=ParseMode.HTML,
            )
            return
        if meta.get("is_live"):
            await status.edit_text(
                f"{CE_STOP} <b>Live streams are not supported.</b>\n"
                "Send the link after the stream ends.",
                parse_mode=ParseMode.HTML,
            )
            return

        uid = uuid.uuid4().hex[:8]
        await store_url(uid, url, ttl=600)
        thumb = meta.get("thumbnail") or meta.get("thumbnail_url", "")
        if thumb:
            await store_url(f"th_{uid}", thumb, ttl=600)

        platform = meta.get("platform", "Unknown")
        dur      = format_duration(meta["duration"]) if meta.get("duration") else ""

        if platform == "Spotify":
            dur_line = f"\n<b>{CE_CLOCK}</b> {dur}" if dur else ""
            text = (
                f"<b>🎧 {meta['title']}</b>\n"
                f"<b>👤</b> {meta['uploader']}{dur_line}\n"
                f"<b>🟢 Spotify</b>  ·  MP3 320kbps\n\n"
                "<b>Tap below to download:</b>"
            )
        elif platform == "Apple Music":
            dur_line = f"\n<b>{CE_CLOCK}</b> {dur}" if dur else ""
            text = (
                f"<b>🍎 {meta['title']}</b>\n"
                f"<b>👤</b> {meta['uploader']}{dur_line}\n"
                f"<b>Apple Music</b>  ·  MP3 320kbps\n\n"
                "<b>Tap below to download:</b>"
            )
        elif platform == "Instagram":
            text = (
                f"<b>📸 Instagram Media</b>\n"
                f"<b>👤</b> {meta['uploader'] or 'Instagram'}\n\n"
                "<b>Tap below to download:</b>"
            )
        else:
            dur_line = f"<b>{CE_CLOCK}</b> {dur}  ·  " if dur else ""
            text = (
                f"<b>▶️ {meta['title']}</b>\n"
                f"<b>👤</b> {meta['uploader']}\n"
                f"{dur_line}<b>📺</b> {platform}\n\n"
                "<b>Choose format &amp; quality:</b>"
            )

        kb = quality_keyboard(meta.get("resolutions", ["best"]), user_id, uid)
        await status.edit_text(
            text, parse_mode=ParseMode.HTML,
            reply_markup=kb, disable_web_page_preview=True,
        )

    # ── Playlist ──────────────────────────────────────────────────────────────

    async def _handle_playlist(client, msg, status, url, user_id):
        pl = await fetch_playlist_metadata(url)
        if not pl:
            meta = await fetch_metadata(url)
            if meta:
                await _handle_single(client, msg, status, url, user_id)
            else:
                await status.edit_text(
                    f"{CE_CROSS} <b>Could not fetch playlist info.</b>",
                    parse_mode=ParseMode.HTML,
                )
            return

        pid = uuid.uuid4().hex[:8]
        await store_url(pid, url, ttl=1800)
        await store_json(f"pl_{pid}", pl, ttl=1800)

        # Pre-warm metadata cache
        for entry in pl.get("entries", []):
            if entry.get("_prefill") and entry.get("url"):
                _cache_set(entry["url"], entry["_prefill"])

        platform   = pl.get("platform", "")
        is_music   = platform in ("Spotify", "Apple Music")
        item_label = "tracks" if is_music else "videos"
        icon       = "🎵" if is_music else "📋"
        kb = quality_keyboard(
            pl.get("resolutions", ["1080p", "720p", "480p", "360p"]),
            user_id, pid, is_playlist=True,
        )
        text = (
            f"<b>{icon} {pl['playlist_title']}</b>\n"
            f"<b>👤</b> {pl['uploader']}  ·  <b>{pl['total']} {item_label}</b>\n\n"
            f"<b>{'Choose quality:' if not is_music else 'Tap below to download:'}</b>"
        )
        await status.edit_text(
            text, parse_mode=ParseMode.HTML,
            reply_markup=kb, disable_web_page_preview=True,
        )

    # ── UI callbacks ──────────────────────────────────────────────────────────

    @app.on_callback_query(filters.regex(r"^ui:"))
    async def cb_ui(client: Client, cb: CallbackQuery):
        action = cb.data.split(":")[1]
        await cb.answer()
        me = await client.get_me()

        if action == "help":
            await cb.message.edit_text(
                HELP_TXT, parse_mode=ParseMode.HTML,
                reply_markup=back_keyboard(), disable_web_page_preview=True,
            )
        elif action == "about":
            await cb.message.edit_text(
                ABOUT_TXT(me.username), parse_mode=ParseMode.HTML,
                reply_markup=back_keyboard(), disable_web_page_preview=True,
            )
        elif action == "stats":
            await _send_stats(client, cb.message.chat.id, edit_msg=cb.message)
        elif action == "start":
            text = START_TXT(
                first_name   = cb.from_user.first_name or "there",
                bot_username = me.username,
                bot_name     = me.first_name,
            )
            try:
                await cb.message.edit_text(
                    text, parse_mode=ParseMode.HTML, reply_markup=start_keyboard()
                )
            except Exception:
                pass

    # ── Playlist quality selected ─────────────────────────────────────────────

    @app.on_callback_query(filters.regex(r"^plq:"))
    async def cb_playlist_quality(client: Client, cb: CallbackQuery):
        _, owner_id, pid, quality = cb.data.split(":")
        owner_id = int(owner_id)
        if cb.from_user.id != owner_id:
            await cb.answer("❌ Not your session!", show_alert=True)
            return
        await cb.answer()

        pl = await load_json(f"pl_{pid}")
        if not pl:
            await cb.message.edit_text(
                f"{CE_CLOCK} <b>Session expired.</b> Please send the link again.",
                parse_mode=ParseMode.HTML,
            )
            return

        text, kb = playlist_keyboard(pl, owner_id, pid, quality, page=0, page_size=_PAGE_SIZE)
        await cb.message.edit_text(
            text, parse_mode=ParseMode.HTML,
            reply_markup=kb, disable_web_page_preview=True,
        )

    # ── Playlist page ─────────────────────────────────────────────────────────

    @app.on_callback_query(filters.regex(r"^plp:"))
    async def cb_playlist_page(client: Client, cb: CallbackQuery):
        _, owner_id, pid, quality, page = cb.data.split(":")
        owner_id, page = int(owner_id), int(page)
        if cb.from_user.id != owner_id:
            await cb.answer("❌ Not your session!", show_alert=True)
            return
        await cb.answer()

        pl = await load_json(f"pl_{pid}")
        if not pl:
            await cb.message.edit_text(
                f"{CE_CLOCK} <b>Session expired.</b>", parse_mode=ParseMode.HTML
            )
            return

        text, kb = playlist_keyboard(pl, owner_id, pid, quality, page, _PAGE_SIZE)
        await cb.message.edit_text(
            text, parse_mode=ParseMode.HTML,
            reply_markup=kb, disable_web_page_preview=True,
        )

    # ── Playlist single item ──────────────────────────────────────────────────

    @app.on_callback_query(filters.regex(r"^pli:"))
    async def cb_playlist_item(client: Client, cb: CallbackQuery):
        _, owner_id, pid, quality, idx = cb.data.split(":")
        owner_id, idx = int(owner_id), int(idx)
        if cb.from_user.id != owner_id:
            await cb.answer("❌ Not your session!", show_alert=True)
            return
        await cb.answer()

        pl = await load_json(f"pl_{pid}")
        if not pl:
            await cb.message.edit_text(
                f"{CE_CLOCK} <b>Session expired.</b>", parse_mode=ParseMode.HTML
            )
            return

        entry  = pl["entries"][idx]
        pushed = await push_job({
            "user_id":    owner_id,
            "chat_id":    cb.message.chat.id,
            "message_id": cb.message.id,
            "url":        entry["url"],
            "quality":    quality,
        })
        if not pushed:
            await cb.answer("⚠️ Queue full! Wait for current jobs.", show_alert=True)
            return

        q_pos = await queue_length()
        await cb.message.edit_text(
            f"{CE_TICK} <b>Queued:</b> {entry['title']}\n"
            f"<b>Quality:</b> <code>{quality}</code>  ·  <b>Position:</b> <code>#{q_pos}</code>\n\n"
            "I'll send it when ready 🚀",
            parse_mode=ParseMode.HTML,
        )

    # ── Playlist download all ─────────────────────────────────────────────────

    @app.on_callback_query(filters.regex(r"^pla:"))
    async def cb_playlist_all(client: Client, cb: CallbackQuery):
        _, owner_id, pid, quality = cb.data.split(":")
        owner_id = int(owner_id)
        if cb.from_user.id != owner_id:
            await cb.answer("❌ Not your session!", show_alert=True)
            return
        await cb.answer()

        pl = await load_json(f"pl_{pid}")
        if not pl:
            await cb.message.edit_text(
                f"{CE_CLOCK} <b>Session expired.</b>", parse_mode=ParseMode.HTML
            )
            return

        entries = pl["entries"][:MAX_PLAYLIST_ALL]
        total   = len(entries)
        await cb.message.edit_text(
            f"⏳ <b>Queuing {total} items...</b>",
            parse_mode=ParseMode.HTML,
        )

        queued = skipped = 0
        for e in entries:
            ok = await push_job({
                "user_id":    owner_id,
                "chat_id":    cb.message.chat.id,
                "message_id": cb.message.id,
                "url":        e["url"],
                "quality":    quality,
            })
            if ok:
                queued += 1
            else:
                skipped += 1

        txt = (
            f"{CE_TICK} <b>{queued} items queued!</b>  Quality: <code>{quality}</code>\n"
            + (f"{CE_WARN} {skipped} skipped (queue limit).\n" if skipped else "")
            + "\nItems will arrive one by one 🚀"
        )
        await cb.message.edit_text(txt, parse_mode=ParseMode.HTML)

    # ── Single video quality chosen ───────────────────────────────────────────

    @app.on_callback_query(filters.regex(r"^dl:"))
    async def cb_download(client: Client, cb: CallbackQuery):
        parts    = cb.data.split(":")
        owner_id = int(parts[1])
        uid      = parts[2]
        quality  = parts[3]
        if cb.from_user.id != owner_id:
            await cb.answer("❌ This isn't your download!", show_alert=True)
            return
        await cb.answer()

        url = await load_url(uid)
        if not url:
            await cb.message.edit_text(
                f"{CE_CLOCK} <b>Expired.</b> Please send the link again.",
                parse_mode=ParseMode.HTML,
            )
            return

        pushed = await push_job({
            "user_id":    owner_id,
            "chat_id":    cb.message.chat.id,
            "message_id": cb.message.id,
            "url":        url,
            "quality":    quality,
        })
        if not pushed:
            await cb.message.edit_text(
                f"{CE_WARN} <b>Queue full!</b> Use /cancel to clear.",
                parse_mode=ParseMode.HTML,
            )
            return

        q_pos = await queue_length()
        await cb.message.edit_text(
            f"{CE_TICK} <b>Added to queue!</b>\n"
            f"<b>Quality:</b> <code>{quality}</code>  ·  <b>Position:</b> <code>#{q_pos}</code>\n\n"
            "Starting soon 🚀",
            parse_mode=ParseMode.HTML,
        )

    # ── Thumbnail ─────────────────────────────────────────────────────────────

    @app.on_callback_query(filters.regex(r"^thumb:"))
    async def cb_thumbnail(client: Client, cb: CallbackQuery):
        parts    = cb.data.split(":")
        owner_id = int(parts[1])
        uid      = parts[2]
        if cb.from_user.id != owner_id:
            await cb.answer("❌ Not your button!", show_alert=True)
            return

        await cb.answer("🖼 Fetching thumbnail...")
        status = await cb.message.edit_text(
            "🖼 <b>Fetching thumbnail...</b>", parse_mode=ParseMode.HTML
        )

        thumb_url = await load_url(f"th_{uid}")
        if not thumb_url:
            url = await load_url(uid)
            if not url:
                await status.edit_text(
                    f"{CE_CLOCK} <b>Expired.</b>", parse_mode=ParseMode.HTML
                )
                return
            meta      = await fetch_metadata(url)
            thumb_url = meta.get("thumbnail") if meta else None

        if not thumb_url:
            await status.edit_text(
                f"{CE_CROSS} <b>No thumbnail found.</b>", parse_mode=ParseMode.HTML
            )
            return

        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as http:
                r   = await http.get(thumb_url)
                r.raise_for_status()
                bio      = BytesIO(r.content)
                bio.name = "thumbnail.jpg"
            await status.delete()
            await client.send_photo(
                chat_id=cb.message.chat.id,
                photo=bio,
                caption="🖼 <b>Thumbnail</b>",
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.error(f"Thumbnail fetch failed: {e}")
            try:
                await status.edit_text(
                    f"{CE_CROSS} <b>Thumbnail error.</b>", parse_mode=ParseMode.HTML
                )
            except Exception:
                pass

    # ── Cancel ────────────────────────────────────────────────────────────────

    @app.on_callback_query(filters.regex(r"^cancel:"))
    async def cb_cancel(client: Client, cb: CallbackQuery):
        owner_id = int(cb.data.split(":")[1])
        if cb.from_user.id != owner_id:
            await cb.answer("❌ Not your button!", show_alert=True)
            return
        await cb.answer("Cancelled!")
        await cb.message.delete()
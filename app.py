"""
app.py — MediaFetch Bot entry point.

Hosts:
  VPS / Docker Compose  →  docker-compose up
  Render                →  render.yaml  (env: docker)
  Railway               →  railway.json (DOCKERFILE builder)
  Heroku                →  Procfile + Aptfile (heroku-community/apt for ffmpeg)
  Fly.io                →  fly.toml

HTTP server on $PORT (default 8080):
  GET /       → 200 "MediaFetch Bot is running"
  GET /ping   → 200 "pong"
  GET /health → 200 JSON stats
"""

import asyncio
import logging
import os
import signal
import sys
import time
from logging.handlers import RotatingFileHandler

from aiohttp import web
from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait

from config import API_ID, API_HASH, BOT_TOKEN, TMP_DIR, LOG_FILE, LOG_DIR, WORKER_SLEEP
from database.mongo import (
    init_indexes, close_mongo, ping,
    pop_job, complete_job,
    set_user_processing, clear_user_processing,
    should_cancel, clear_cancel, increment_stat,
)
from modules.handlers import register_handlers
from modules.download import download_media
from modules.uploader import upload_media
from modules.util import ensure_tmp_dir, cleanup_user_tmp
from modules.ping import keep_alive_loop, detect_platform

# ── Logging ───────────────────────────────────────────────────────────────────
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler(
            LOG_FILE, maxBytes=5 * 1024 * 1024,
            backupCount=3, encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger("app")
logging.getLogger("pyrogram").setLevel(logging.WARNING)
logging.getLogger("motor").setLevel(logging.WARNING)
logging.getLogger("aiohttp").setLevel(logging.WARNING)

# ── HTTP server ───────────────────────────────────────────────────────────────

async def start_http_server():
    """
    Tiny aiohttp server.
    All hosting platforms need an HTTP endpoint alive for health checks.
    PORT env var is set automatically by Render / Railway / Heroku / Fly.
    """
    port = int(os.getenv("PORT", "8080"))

    async def handle_root(request):
        return web.Response(text="MediaFetch Bot is running ✅")

    async def handle_ping(request):
        return web.Response(text="pong")

    async def handle_health(request):
        from database.mongo import get_stats, queue_length
        stats = await get_stats()
        q     = await queue_length()
        return web.json_response({**stats, "queue": q, "status": "ok"})

    app_http = web.Application()
    app_http.router.add_get("/",       handle_root)
    app_http.router.add_get("/ping",   handle_ping)
    app_http.router.add_get("/health", handle_health)

    runner = web.AppRunner(app_http)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"HTTP server listening on 0.0.0.0:{port} ✓")
    return runner


# ── Job processor ─────────────────────────────────────────────────────────────

async def process_job(bot: Client, job: dict):
    user_id    = job["user_id"]
    chat_id    = job["chat_id"]
    url        = job["url"]
    quality    = job.get("quality", "best")
    job_id     = job.get("_id")
    status_msg = None
    success    = False

    try:
        await set_user_processing(user_id, url)

        from modules.download import (
            _is_youtube, _is_spotify, _is_apple_music, _is_instagram,
        )
        from modules.ui import download_start_text, format_progress as _fmt_prog

        _platform = (
            "YouTube"     if _is_youtube(url)     else
            "Spotify"     if _is_spotify(url)     else
            "Apple Music" if _is_apple_music(url) else
            "Instagram"   if _is_instagram(url)   else
            "Unknown"
        )

        status_msg = await bot.send_message(
            chat_id,
            download_start_text(url, quality, _platform),
            parse_mode=ParseMode.HTML,
        )

        if await should_cancel(user_id):
            await clear_cancel(user_id)
            await status_msg.edit_text("🛑 <b>Download cancelled.</b>", parse_mode=ParseMode.HTML)
            return

        _last_edit  = [0.0]
        _last_bytes = [0]
        _t0         = [time.monotonic()]

        async def on_progress(downloaded: int, total: int):
            now = time.monotonic()
            if now - _last_edit[0] < 3:
                return
            speed          = (downloaded - _last_bytes[0]) / max(now - _last_edit[0], 0.001)
            elapsed        = now - _t0[0]
            _last_edit[0]  = now
            _last_bytes[0] = downloaded
            try:
                await status_msg.edit_text(
                    "📥 <b>Downloading...</b>\n\n" + _fmt_prog(downloaded, total, speed, elapsed),
                    parse_mode=ParseMode.HTML,
                )
            except FloodWait as e:
                await asyncio.sleep(e.value)
            except Exception:
                pass

        await status_msg.edit_text("📥 <b>Downloading...</b>", parse_mode=ParseMode.HTML)

        if await should_cancel(user_id):
            await clear_cancel(user_id)
            await status_msg.edit_text("🛑 <b>Download cancelled.</b>", parse_mode=ParseMode.HTML)
            return

        result = await download_media(
            url=url, user_id=user_id,
            quality=quality, progress_callback=on_progress,
        )

        if result is None:
            await status_msg.edit_text(
                "❌ <b>Download failed.</b> File could not be retrieved.",
                parse_mode=ParseMode.HTML,
            )
            await increment_stat("total_errors")
            return

        result["user_id"] = user_id

        original_msg = await bot.get_messages(chat_id, job.get("message_id"))
        success = await upload_media(
            client=bot, msg=original_msg,
            status_msg=status_msg, download_result=result,
        )

        if success:
            await increment_stat("total_downloads")
            await increment_stat("total_bytes", int(result.get("size_mb", 0) * 1024 * 1024))
            logger.info(f"Job done: user={user_id} size={result.get('size_mb', 0):.1f}MB")
        else:
            await increment_stat("total_errors")

    except ValueError as e:
        logger.warning(f"Download error user={user_id}: {e}")
        await increment_stat("total_errors")
        try:
            if status_msg:
                await status_msg.edit_text(f"❌ <b>Error:</b> {e}", parse_mode=ParseMode.HTML)
            else:
                await bot.send_message(chat_id, f"❌ <b>Error:</b> {e}", parse_mode=ParseMode.HTML)
        except Exception:
            pass

    except FloodWait as e:
        logger.warning(f"FloodWait {e.value}s user={user_id}")
        await asyncio.sleep(e.value)

    except Exception as e:
        logger.exception(f"Unexpected error user={user_id}: {e}")
        await increment_stat("total_errors")
        try:
            if status_msg:
                await status_msg.edit_text(
                    "💥 <b>Unexpected error.</b> Please try again.",
                    parse_mode=ParseMode.HTML,
                )
        except Exception:
            pass

    finally:
        await clear_user_processing(user_id)
        await clear_cancel(user_id)
        if job_id:
            await complete_job(job_id, success=success)
        cleanup_user_tmp(user_id)


# ── Worker loop ───────────────────────────────────────────────────────────────

async def worker_loop(bot: Client):
    logger.info("Worker loop started.")
    while True:
        try:
            job = await pop_job()
            if job:
                asyncio.create_task(process_job(bot, job))
            else:
                await asyncio.sleep(WORKER_SLEEP)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Worker loop error: {e}")
            await asyncio.sleep(5)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    if not all([API_ID, API_HASH, BOT_TOKEN]):
        logger.critical("Missing API_ID, API_HASH, or BOT_TOKEN — check your environment variables!")
        sys.exit(1)

    ensure_tmp_dir()

    platform = detect_platform()
    logger.info(f"Hosting platform: {platform}")

    # MongoDB
    if not await ping():
        logger.critical("Cannot connect to MongoDB — check MONGO_URI!")
        sys.exit(1)
    logger.info("MongoDB connected ✓")
    await init_indexes()

    # HTTP server (required by every hosting platform for health checks)
    http_runner = await start_http_server()

    # Bot
    bot = Client(
        name="mediafetch_bot",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
    )
    register_handlers(bot)

    worker_task = None
    ping_task   = None

    async def shutdown(sig_name=""):
        if sig_name:
            logger.info(f"Received {sig_name} — shutting down...")
        else:
            logger.info("Shutting down...")
        for t in [worker_task, ping_task]:
            if t and not t.done():
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
        try:
            await bot.stop()
        except Exception:
            pass
        await close_mongo()
        try:
            await http_runner.cleanup()
        except Exception:
            pass
        logger.info("Shutdown complete.")

    # Signal handlers (SIGINT = Ctrl-C, SIGTERM = platform shutdown)
    if sys.platform != "win32":
        loop = asyncio.get_running_loop()
        for sig, name in [(signal.SIGINT, "SIGINT"), (signal.SIGTERM, "SIGTERM")]:
            loop.add_signal_handler(
                sig,
                lambda n=name: asyncio.create_task(shutdown(n)),
            )

    logger.info("Starting Pyrogram bot...")
    await bot.start()
    me = await bot.get_me()
    logger.info(f"Bot started: @{me.username} ✓")

    worker_task = asyncio.create_task(worker_loop(bot))
    ping_task   = asyncio.create_task(keep_alive_loop())

    logger.info("Bot is running. Press Ctrl+C to stop.")
    try:
        await asyncio.gather(worker_task, ping_task)
    except (KeyboardInterrupt, asyncio.CancelledError):
        await shutdown()


if __name__ == "__main__":
    asyncio.run(main())

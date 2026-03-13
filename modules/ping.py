"""
modules/ping.py
Self-ping / keep-alive for free hosting platforms.

Detects hosting environment automatically:
  - Render.com   → RENDER env var present
  - Heroku       → DYNO env var present
  - Railway      → RAILWAY_ENVIRONMENT present
  - Fly.io       → FLY_APP_NAME present
  - VPS/local    → none of the above (ping disabled)

When an APP_URL is set (or auto-detected), the bot pings its own HTTP
endpoint every PING_INTERVAL seconds to prevent free-tier sleep/cold start.
"""

import asyncio
import logging
import os

import httpx

from config import APP_URL, PING_INTERVAL

logger = logging.getLogger(__name__)


def detect_platform() -> str:
    """Return the name of the detected hosting platform."""
    if os.getenv("RENDER"):
        return "Render"
    if os.getenv("DYNO"):
        return "Heroku"
    if os.getenv("RAILWAY_ENVIRONMENT"):
        return "Railway"
    if os.getenv("FLY_APP_NAME"):
        return "Fly.io"
    if os.getenv("WEBSITE_INSTANCE_ID"):     # Azure App Service
        return "Azure"
    if os.getenv("GAE_APPLICATION"):         # Google App Engine
        return "GCP App Engine"
    return "VPS/Local"


def get_app_url() -> str:
    """
    Return the URL to ping. Tries APP_URL env var first, then
    auto-constructs from platform-specific variables.
    """
    if APP_URL:
        return APP_URL.rstrip("/")

    # Render auto-sets RENDER_EXTERNAL_URL
    if os.getenv("RENDER_EXTERNAL_URL"):
        return os.getenv("RENDER_EXTERNAL_URL").rstrip("/")

    # Railway auto-sets RAILWAY_PUBLIC_DOMAIN
    if os.getenv("RAILWAY_PUBLIC_DOMAIN"):
        return f"https://{os.getenv('RAILWAY_PUBLIC_DOMAIN')}"

    # Heroku auto-sets HEROKU_APP_NAME
    if os.getenv("HEROKU_APP_NAME"):
        return f"https://{os.getenv('HEROKU_APP_NAME')}.herokuapp.com"

    return ""


async def keep_alive_loop():
    """
    Background coroutine that pings the app URL periodically.
    Exits silently if no URL is configured or running on VPS.
    """
    platform = detect_platform()
    url      = get_app_url()

    if not url:
        logger.info(f"Platform: {platform} — Self-ping disabled (no APP_URL).")
        return

    logger.info(f"Platform: {platform} — Self-ping enabled → {url} every {PING_INTERVAL}s")

    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        while True:
            try:
                await asyncio.sleep(PING_INTERVAL)
                r = await client.get(f"{url}/ping")
                logger.debug(f"Self-ping → {r.status_code}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Self-ping failed (non-fatal): {e}")

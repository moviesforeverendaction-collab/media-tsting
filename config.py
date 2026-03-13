"""
config.py — MediaFetch Bot configuration.
Supported: YouTube · Spotify · Apple Music · Instagram
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── Telegram ──────────────────────────────────────────────────────────────────
API_ID    = int(os.getenv("API_ID", "34857357"))
API_HASH  = os.getenv("API_HASH", "1e8f2a02989b22ef1e55340375bbdaa8")
BOT_TOKEN = os.getenv("BOT_TOKEN", "7888814734:AAG3cfiTkIvm6NzIVZhpPv7SB3JNOWbpD_k")

# ── MongoDB ───────────────────────────────────────────────────────────────────
MONGO_URI = os.getenv("MONGO_URI", "mongodb+srv://LastPerson07:N7z0DRcklsZzqCzd@storagebot.5fuk3xn.mongodb.net/?appName=StorageBot")
MONGO_DB  = os.getenv("MONGO_DB",  "mediafetch")

# ── Spotify (spotDL) ──────────────────────────────────────────────────────────
SPOTDL_BIN = os.getenv("SPOTDL_BIN", "spotdl")

# ── Apple Music ───────────────────────────────────────────────────────────────
# No tokens or cookies needed.
# Flow: iTunes API (metadata) → YouTube Music search → yt-dlp (download)
AM_STOREFRONT = os.getenv("AM_STOREFRONT", "us")

# ── Limits ────────────────────────────────────────────────────────────────────
MAX_FILE_SIZE_MB   = int(os.getenv("MAX_FILE_SIZE_MB",   "2000"))
RATE_LIMIT_COUNT   = int(os.getenv("RATE_LIMIT_COUNT",   "5"))
RATE_LIMIT_WINDOW  = int(os.getenv("RATE_LIMIT_WINDOW",  "60"))
MAX_QUEUE_PER_USER = int(os.getenv("MAX_QUEUE_PER_USER", "3"))
SPLIT_THRESHOLD_MB = int(os.getenv("SPLIT_THRESHOLD_MB", "1900"))
SPLIT_PART_MB      = int(os.getenv("SPLIT_PART_MB",      "1900"))
MAX_PLAYLIST_ALL   = int(os.getenv("MAX_PLAYLIST_ALL",   "50"))

# ── Supported domains ─────────────────────────────────────────────────────────
SUPPORTED_DOMAINS = [
    "youtube.com", "youtu.be", "youtube-nocookie.com",
    "open.spotify.com",
    "music.apple.com",
    "instagram.com",
]

# ── Paths ─────────────────────────────────────────────────────────────────────
TMP_DIR  = os.path.join(os.path.dirname(__file__), "tmp")
LOG_DIR  = os.path.join(os.path.dirname(__file__), "logs")
LOG_FILE = os.path.join(LOG_DIR, "bot.log")

# ── Worker ────────────────────────────────────────────────────────────────────
QUEUE_NAME   = "mediafetch_queue"
WORKER_SLEEP = 1

# ── Self-ping ─────────────────────────────────────────────────────────────────
APP_URL       = os.getenv("APP_URL", "")
PING_INTERVAL = int(os.getenv("PING_INTERVAL", "300"))

# ── Wallpaper ─────────────────────────────────────────────────────────────────
STATIC_WALLPAPER = os.getenv(
    "STATIC_WALLPAPER",
    "https://images.unsplash.com/photo-1518770660439-4636190af475?w=800&q=80",
)

# ── Custom Emoji IDs ──────────────────────────────────────────────────────────
CE_TICK      = '<emoji id=5206607081334906820>✔️</emoji>'
CE_CROSS     = '<emoji id=5210952531676504517>❌</emoji>'
CE_DL        = '<emoji id=5406745015365943482>⬇️</emoji>'
CE_GEAR      = '<emoji id=5341715473882955310>⚙️</emoji>'
CE_MUSIC     = '<emoji id=5463107823946717464>🎵</emoji>'
CE_CLOCK     = '<emoji id=5386367538735104399>⌛</emoji>'
CE_SEARCH    = '<emoji id=5231012545799666522>🔍</emoji>'
CE_LIGHTNING = '<emoji id=5456140674028019486>⚡️</emoji>'
CE_FIRE      = '<emoji id=5424972470023104089>🔥</emoji>'
CE_CHART     = '<emoji id=5231200819986047254>📊</emoji>'
CE_SPARKLE   = '<emoji id=5325547803936572038>✨</emoji>'
CE_GEM       = '<emoji id=5427168083074628963>💎</emoji>'
CE_CROWN     = '<emoji id=5217822164362739968>👑</emoji>'
CE_HOME      = '<emoji id=5416041192905265756>🏠</emoji>'
CE_GLOBE     = '<emoji id=5447410659077661506>🌐</emoji>'
CE_WARN      = '<emoji id=5447644880824181073>⚠️</emoji>'
CE_GREEN     = '<emoji id=5416081784641168838>🟢</emoji>'
CE_RED       = '<emoji id=5411225014148014586>🔴</emoji>'
CE_PARTY     = '<emoji id=5461151367559141950>🎉</emoji>'
CE_REFRESH   = '<emoji id=5375338737028841420>🔄</emoji>'
CE_SHIELD    = '<emoji id=5251203410396458957>🛡</emoji>'
CE_LINK      = '<emoji id=5271604874419647061>🔗</emoji>'
CE_PC        = '<emoji id=5282843764451195532>🖥</emoji>'
CE_INFO      = '<emoji id=5334544901428229844>ℹ️</emoji>'
CE_BELL      = '<emoji id=5458603043203327669>🔔</emoji>'
CE_CLIP      = '<emoji id=5305265301917549162>📎</emoji>'
CE_PLAY      = '<emoji id=5264919878082509254>▶️</emoji>'
CE_STAR      = '<emoji id=5438496463044752972>⭐️</emoji>'
CE_STOP      = '<emoji id=5260293700088511294>⛔️</emoji>'
CE_EXCL      = '<emoji id=5274099962655816924>❗️</emoji>'
CE_QUESTION  = '<emoji id=5436113877181941026>❓</emoji>'
CE_PAUSE     = '<emoji id=5359543311897998264>⏸</emoji>'
CE_HUNDRED   = '<emoji id=5341498088408234504>💯</emoji>'
CE_TRASH     = '<emoji id=5445267414562389170>🗑</emoji>'
CE_ARROW     = '<emoji id=5416117059207572332>➡️</emoji>'
CE_NEW       = '<emoji id=5382357040008021292>🆕</emoji>'
CE_PIN       = '<emoji id=5391032818111363540>📍</emoji>'
CE_PHOTO     = '<emoji id=5406683434124859552>🛍</emoji>'
CE_UP        = '<emoji id=5449683594425410231>🔼</emoji>'
CE_DOWN      = '<emoji id=5447183459602669338>🔽</emoji>'

# ── Reaction set ──────────────────────────────────────────────────────────────
REACTIONS = [
    "👍", "❤️", "🔥", "🥰", "👏", "😁", "🎉", "🤩",
    "💯", "⚡", "🏆", "😍", "🌚", "✨", "👌", "🙏",
]

# ── Text templates ────────────────────────────────────────────────────────────
def START_TXT(first_name: str, bot_username: str, bot_name: str) -> str:
    return (
        f"<b>{CE_CROWN} Hey {first_name}!</b>\n"
        f"<b>{CE_GLOBE} <a href='https://t.me/{bot_username}'>{bot_name}</a></b> — your high-speed media downloader.\n\n"
        "<blockquote>"
        f"{CE_GREEN} <b>Online</b>  ·  {CE_LIGHTNING} <b>Fast</b>  ·  {CE_SHIELD} <b>Private</b>"
        "</blockquote>\n\n"
        "<blockquote>"
        f"{CE_PLAY} <b>YouTube</b>  —  videos, shorts, playlists\n"
        f"{CE_MUSIC} <b>Spotify</b>  —  tracks, albums, playlists\n"
        f"{CE_MUSIC} <b>Apple Music</b>  —  songs, albums, playlists\n"
        f"{CE_PHOTO} <b>Instagram</b>  —  posts, reels, stories"
        "</blockquote>\n\n"
        f"{CE_CLIP} <b>Paste any supported link to get started!</b>"
    )


HELP_TXT = (
    f"<b>{CE_INFO} Help Guide</b>\n\n"
    "<blockquote>"
    f"<b>{CE_PLAY} YouTube</b>\n"
    "Videos, Shorts, Playlists — choose quality from 360p up to 4K\n"
    "Audio-only (MP3) also available"
    "</blockquote>\n\n"
    "<blockquote>"
    f"<b>{CE_MUSIC} Spotify</b>\n"
    "Tracks, Albums, Playlists → downloaded as MP3 320kbps via spotDL"
    "</blockquote>\n\n"
    "<blockquote>"
    f"<b>{CE_MUSIC} Apple Music</b>\n"
    "Songs, Albums, Playlists → matched on YouTube Music → downloaded as MP3 320kbps"
    "</blockquote>\n\n"
    "<blockquote>"
    f"<b>{CE_PHOTO} Instagram</b>\n"
    "Posts, Reels, Stories — photos and videos"
    "</blockquote>\n\n"
    "<blockquote>"
    f"<b>{CE_GEAR} Commands</b>\n"
    "/start — home  ·  /help — this guide\n"
    "/stats — bot stats  ·  /cancel — stop active download\n"
    f"/ping — latency check  ·  /about — bot info"
    "</blockquote>\n\n"
    "<blockquote>"
    f"<b>{CE_WARN} Limits</b>\n"
    f"Max file size: <b>{MAX_FILE_SIZE_MB} MB</b>  ·  Large files are <b>auto-split</b>"
    "</blockquote>"
)


def ABOUT_TXT(bot_username: str) -> str:
    return (
        f"<b>{CE_INFO} About MediaFetch</b>\n\n"
        "<blockquote>"
        f"{CE_PC} <b>Bot:</b> <a href='http://t.me/{bot_username}'>@{bot_username}</a>\n"
        f"{CE_LINK} <b>Framework:</b> Pyrogram (async MTProto)\n"
        f"{CE_SPARKLE} <b>Language:</b> Python 3.11+\n"
        f"{CE_SHIELD} <b>Database:</b> MongoDB Atlas\n"
        f"{CE_DL} <b>Downloader:</b> yt-dlp + spotDL\n"
        f"{CE_MUSIC} <b>Platforms:</b> YouTube · Spotify · Apple Music · Instagram"
        "</blockquote>\n\n"
        f"<blockquote>{CE_SHIELD} Files are never stored. Downloads are processed in memory and sent directly to you.</blockquote>"
    )


UNSUPPORTED_TXT = (
    f"{CE_STOP} <b>Unsupported platform.</b>\n\n"
    "We currently support:\n"
    f"  {CE_PLAY} YouTube\n"
    f"  {CE_MUSIC} Spotify\n"
    f"  {CE_MUSIC} Apple Music\n"
    f"  {CE_PHOTO} Instagram\n\n"
    f"{CE_BELL} Want support for another platform? Use /request to suggest it!"
)
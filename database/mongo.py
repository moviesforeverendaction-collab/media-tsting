"""
database/mongo.py
All database operations using MongoDB (Motor async driver).

Collections:
  - jobs        : download job queue
  - rate_limits : sliding-window rate limiting
  - user_locks  : active processing locks
  - cancel_flags: cancellation signals
  - url_store   : short-ID → full URL mapping (replaces Redis key-value)
  - stats       : bot-wide counters
  - users       : unique user registry
"""

import time
import logging
from typing import Optional

import motor.motor_asyncio
from pymongo import ASCENDING, DESCENDING

from config import (
    MONGO_URI, MONGO_DB,
    RATE_LIMIT_COUNT, RATE_LIMIT_WINDOW,
    MAX_QUEUE_PER_USER,
    QUEUE_NAME,
)

logger = logging.getLogger(__name__)

# ── Singleton client ──────────────────────────────────────────────────────────
_client: Optional[motor.motor_asyncio.AsyncIOMotorClient] = None
_db     = None


def get_db():
    global _client, _db
    if _db is None:
        _client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
        _db     = _client[MONGO_DB]
    return _db


async def close_mongo():
    global _client, _db
    if _client:
        _client.close()
        _client = None
        _db     = None


async def init_indexes():
    """Create indexes on startup — safe to call multiple times."""
    db = get_db()
    # jobs: ordered queue pop by inserted time
    await db.jobs.create_index([("status", ASCENDING), ("created_at", ASCENDING)])
    # rate_limits: auto-expire old entries
    await db.rate_limits.create_index("ts", expireAfterSeconds=RATE_LIMIT_WINDOW)
    # user_locks: auto-expire after 10 min
    await db.user_locks.create_index("expires_at", expireAfterSeconds=0)
    # cancel_flags: auto-expire after 2 min
    await db.cancel_flags.create_index("expires_at", expireAfterSeconds=0)
    # url_store: auto-expire after 10 min
    await db.url_store.create_index("expires_at", expireAfterSeconds=0)
    # json_store: auto-expire (playlists, large data)
    await db.json_store.create_index("expires_at", expireAfterSeconds=0)
    logger.info("MongoDB indexes ensured ✓")


async def ping() -> bool:
    try:
        await get_db().command("ping")
        return True
    except Exception:
        return False


# ── Job queue ─────────────────────────────────────────────────────────────────

async def push_job(job: dict) -> bool:
    """
    Push a download job. Returns False if user already has MAX_QUEUE_PER_USER pending jobs.
    """
    db      = get_db()
    user_id = job["user_id"]

    pending = await db.jobs.count_documents({"user_id": user_id, "status": "pending"})
    if pending >= MAX_QUEUE_PER_USER:
        return False

    await db.jobs.insert_one({
        **job,
        "status":     "pending",
        "created_at": time.time(),
    })
    return True


async def pop_job() -> Optional[dict]:
    """
    Atomically pop the oldest pending job and mark it as processing.
    Returns None if queue is empty.
    """
    db  = get_db()
    doc = await db.jobs.find_one_and_update(
        {"status": "pending"},
        {"$set": {"status": "processing", "started_at": time.time()}},
        sort=[("created_at", ASCENDING)],
        return_document=True,
    )
    if doc:
        doc["_id"] = str(doc["_id"])
    return doc


async def complete_job(job_id: str, success: bool = True):
    """Mark a job as done or failed."""
    from bson import ObjectId
    db = get_db()
    await db.jobs.update_one(
        {"_id": ObjectId(job_id)},
        {"$set": {
            "status":       "done" if success else "failed",
            "completed_at": time.time(),
        }},
    )


async def decrement_pending(user_id: int):
    """No-op — MongoDB status field handles this automatically."""
    pass   # kept for API compatibility with old Redis calls


async def queue_length() -> int:
    return await get_db().jobs.count_documents({"status": "pending"})


# ── Rate limiting ─────────────────────────────────────────────────────────────

async def check_rate_limit(user_id: int) -> tuple[bool, int]:
    """
    Sliding-window rate limit.
    Returns (allowed, retry_after_seconds).
    """
    db         = get_db()
    now        = time.time()
    window_start = now - RATE_LIMIT_WINDOW

    # Count requests in the current window
    count = await db.rate_limits.count_documents({
        "user_id": user_id,
        "ts":      {"$gte": window_start},
    })

    if count >= RATE_LIMIT_COUNT:
        # Find the oldest entry to calculate when the window opens up
        oldest = await db.rate_limits.find_one(
            {"user_id": user_id, "ts": {"$gte": window_start}},
            sort=[("ts", ASCENDING)],
        )
        retry_after = int(RATE_LIMIT_WINDOW - (now - oldest["ts"])) + 1 if oldest else RATE_LIMIT_WINDOW
        return False, retry_after

    # Record this request
    await db.rate_limits.insert_one({
        "user_id": user_id,
        "ts":      now,
        # TTL index uses this field
        "expires_at": now + RATE_LIMIT_WINDOW,
    })
    return True, 0


# ── User processing lock ──────────────────────────────────────────────────────

async def set_user_processing(user_id: int, url: str):
    db = get_db()
    await db.user_locks.update_one(
        {"user_id": user_id},
        {"$set": {"url": url, "expires_at": time.time() + 600}},
        upsert=True,
    )


async def clear_user_processing(user_id: int):
    await get_db().user_locks.delete_one({"user_id": user_id})


async def is_user_processing(user_id: int) -> bool:
    doc = await get_db().user_locks.find_one({"user_id": user_id})
    return doc is not None


# ── Cancellation ──────────────────────────────────────────────────────────────

async def cancel_user_job(user_id: int):
    db = get_db()
    await db.cancel_flags.update_one(
        {"user_id": user_id},
        {"$set": {"expires_at": time.time() + 120}},
        upsert=True,
    )


async def should_cancel(user_id: int) -> bool:
    doc = await get_db().cancel_flags.find_one({"user_id": user_id})
    return doc is not None


async def clear_cancel(user_id: int):
    await get_db().cancel_flags.delete_one({"user_id": user_id})


# ── URL store (replaces Redis key-value for button callback data) ─────────────

async def store_url(uid: str, url: str, ttl: int = 600):
    """Store a URL under a short ID for use in inline button callback_data."""
    db = get_db()
    await db.url_store.update_one(
        {"uid": uid},
        {"$set": {"url": url, "expires_at": time.time() + ttl}},
        upsert=True,
    )


async def load_url(uid: str) -> Optional[str]:
    """Retrieve a URL by its short ID."""
    doc = await get_db().url_store.find_one({"uid": uid})
    return doc["url"] if doc else None


# ── Stats ─────────────────────────────────────────────────────────────────────

async def increment_stat(key: str, amount: int = 1):
    await get_db().stats.update_one(
        {"key": key},
        {"$inc": {"value": amount}},
        upsert=True,
    )


async def get_stats() -> dict:
    db   = get_db()
    keys = ["total_downloads", "total_users", "total_errors", "total_bytes"]
    docs = await db.stats.find({"key": {"$in": keys}}).to_list(length=10)
    result = {k: 0 for k in keys}
    for doc in docs:
        result[doc["key"]] = doc.get("value", 0)
    return result


async def register_user(user_id: int):
    """Track unique users."""
    db      = get_db()
    existed = await db.users.find_one({"user_id": user_id})
    if not existed:
        await db.users.insert_one({"user_id": user_id, "joined_at": time.time()})
        await increment_stat("total_users")


# ── JSON store (for playlist data — too large for url_store) ──────────────────

async def store_json(uid: str, data: dict, ttl: int = 1800):
    """Store arbitrary JSON data under a short ID. TTL in seconds."""
    import json as _json
    db = get_db()
    await db.json_store.update_one(
        {"uid": uid},
        {"$set": {"data": _json.dumps(data), "expires_at": time.time() + ttl}},
        upsert=True,
    )


async def load_json(uid: str) -> Optional[dict]:
    """Retrieve JSON data by short ID. Returns None if not found/expired."""
    import json as _json
    doc = await get_db().json_store.find_one({"uid": uid})
    if doc and doc.get("data"):
        try:
            return _json.loads(doc["data"])
        except Exception:
            return None
    return None

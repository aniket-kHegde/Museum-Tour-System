"""
services/shared/redis_client.py

Thin wrapper around redis-py for session state shared across all services.
"""

import json
import os
from typing import Any

import redis
from loguru import logger

_client: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            password=os.getenv("REDIS_PASSWORD", None) or None,
            decode_responses=True,
        )
    return _client


# ── Session helpers ───────────────────────────────────────────────────────────

SESSION_TTL = 4 * 60 * 60  # 4 hours


def set_device_session(device_id: str, data: dict):
    """Store device session (exhibit context etc.) with 4h TTL."""
    key = f"session:{device_id}"
    get_redis().setex(key, SESSION_TTL, json.dumps(data))


def get_device_session(device_id: str) -> dict | None:
    key = f"session:{device_id}"
    raw = get_redis().get(key)
    if raw:
        return json.loads(raw)
    return None


def update_session_field(device_id: str, field: str, value: Any):
    session = get_device_session(device_id) or {}
    session[field] = value
    set_device_session(device_id, session)


# ── Beacon cache helpers ──────────────────────────────────────────────────────

BEACON_CACHE_TTL = 60 * 60  # 1 hour


def cache_beacon_content(museum_id: str, beacon_uuid: str, data: dict):
    key = f"beacon_cache:{museum_id}:{beacon_uuid}"
    get_redis().setex(key, BEACON_CACHE_TTL, json.dumps(data))


def get_cached_beacon(museum_id: str, beacon_uuid: str) -> dict | None:
    key = f"beacon_cache:{museum_id}:{beacon_uuid}"
    raw = get_redis().get(key)
    if raw:
        return json.loads(raw)
    return None


# ── Rate limiting ─────────────────────────────────────────────────────────────

VOICE_RATE_LIMIT = 10   # max queries per minute per device
VOICE_RATE_WINDOW = 60  # seconds


def check_voice_rate_limit(device_id: str) -> bool:
    """Returns True if allowed, False if rate-limited."""
    key = f"rate_limit:{device_id}:voice"
    r = get_redis()
    count = r.incr(key)
    if count == 1:
        r.expire(key, VOICE_RATE_WINDOW)
    if count > VOICE_RATE_LIMIT:
        logger.warning(f"Rate limit hit for device {device_id} ({count} queries/min)")
        return False
    return True

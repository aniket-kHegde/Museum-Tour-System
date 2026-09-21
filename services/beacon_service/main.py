"""
services/beacon_service/main.py

Subscribes to beacon_enter events.
Looks up exhibit content from PostgreSQL.
Publishes TTS script back to the device.
Also caches in Redis and publishes retained message to beacon topic.
"""

import asyncio
import json
import os
import sys
import time

import asyncpg
from dotenv import load_dotenv
from loguru import logger

# Allow importing from sibling shared/ directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from shared.mqtt_subscriber import MQTTSubscriberService
from shared.metrics import timed
from shared.redis_client import (
    cache_beacon_content,
    get_cached_beacon,
    set_device_session,
    get_device_session,
)

load_dotenv()

POSTGRES_DSN = (
    f"postgresql://{os.getenv('POSTGRES_USER', 'museum')}"
    f":{os.getenv('POSTGRES_PASSWORD', '')}"
    f"@{os.getenv('POSTGRES_HOST', 'localhost')}"
    f":{os.getenv('POSTGRES_PORT', '5432')}"
    f"/{os.getenv('POSTGRES_DB', 'museum_tour')}"
)


class BeaconContentService(MQTTSubscriberService):
    """
    Handles beacon proximity events.
    Topic: museum/+/device/+/cmd
    Filters for event == 'beacon_enter'.
    """

    TOPICS = ["museum/+/device/+/cmd"]

    def __init__(self, db_pool: asyncpg.Pool):
        super().__init__()
        self.db_pool = db_pool
        self.loop = asyncio.get_event_loop()

    def handle_message(self, topic: str, payload: dict) -> dict | None:
        if payload.get("event") != "beacon_enter":
            return None

        beacon_uuid = payload.get("beacon_uuid")
        museum_id = payload.get("museum_id")
        device_id = payload.get("device_id")
        session_id = payload.get("session_id")

        if not all([beacon_uuid, museum_id, device_id]):
            logger.warning(f"Incomplete beacon_enter payload: {payload}")
            return None

        timings: dict = {}
        meta: dict = {}

        # Try Redis cache first (fast path)
        with timed(timings, "cache_lookup_ms"):
            cached = get_cached_beacon(museum_id, beacon_uuid)
        if cached:
            logger.info(f"Cache hit for beacon {beacon_uuid}")
            meta["source"] = "cache"
            self._update_session(device_id, museum_id, cached, session_id)
            self._publish_retained(museum_id, beacon_uuid, cached)
            return {
                "response_type": "tts",
                "text": cached["tts_script"],
                "exhibit_id": cached["exhibit_id"],
                "_timings": timings,
                "_meta": meta,
            }

        meta["source"] = "db"
        # DB lookup (runs in event loop)
        future = asyncio.run_coroutine_threadsafe(
            self._fetch_exhibit(museum_id, beacon_uuid), self.loop
        )
        _t0 = time.perf_counter()
        try:
            exhibit = future.result(timeout=3.0)
            timings["db_ms"] = round((time.perf_counter() - _t0) * 1000.0, 2)
        except TimeoutError:
            logger.error("DB query timed out for beacon lookup")
            meta["stage"] = "db_timeout"
            return {
                "response_type": "error",
                "text": "Sorry, I could not load information for this exhibit. Please try again.",
                "_timings": timings,
                "_meta": meta,
            }

        if not exhibit:
            logger.warning(f"Unknown beacon UUID: {beacon_uuid} in museum {museum_id}")
            meta["source"] = "unknown_beacon"
            return {
                "response_type": "tts",
                "text": "Welcome! I don't have specific information about this exhibit, but feel free to ask me anything.",
                "exhibit_id": None,
                "_timings": timings,
                "_meta": meta,
            }

        data = {
            "exhibit_id": exhibit["id"],
            "tts_script": exhibit["tts_script"],
            "title": exhibit["title"],
            "author": exhibit.get("author"),
            "year": exhibit.get("year_created"),
        }

        # Cache in Redis and publish retained
        cache_beacon_content(museum_id, beacon_uuid, data)
        self._update_session(device_id, museum_id, data, session_id)
        self._publish_retained(museum_id, beacon_uuid, data)

        logger.info(f"Beacon trigger: device={device_id} exhibit='{exhibit['title']}'")

        return {
            "response_type": "tts",
            "text": exhibit["tts_script"],
            "exhibit_id": exhibit["id"],
            "exhibit_title": exhibit["title"],
            "_timings": timings,
            "_meta": meta,
        }

    async def _fetch_exhibit(self, museum_id: str, beacon_uuid: str) -> asyncpg.Record | None:
        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT e.id, e.title, e.author, e.year_created, e.tts_script
                FROM beacons b
                JOIN exhibits e ON e.id = b.exhibit_id AND e.museum_id = b.museum_id
                WHERE b.uuid = $1 AND b.museum_id = $2
                """,
                beacon_uuid,
                museum_id,
            )
        return row

    def _update_session(
        self, device_id: str, museum_id: str, data: dict, session_id: str | None
    ):
        # Fast, ephemeral session state in Redis (read by qa/vision services).
        session = get_device_session(device_id) or {}
        session.update({
            "museum_id": museum_id,
            "exhibit_id": data["exhibit_id"],
            "exhibit_title": data.get("title"),
            # vision_service reads these two; before this they were never written,
            # so every photo description said "unknown author (unknown year)".
            "exhibit_author": data.get("author"),
            "exhibit_year": data.get("year"),
        })
        set_device_session(device_id, session)

        # Durable tour history in Postgres (best-effort; never blocks the response).
        if session_id:
            self._schedule_db(
                self._persist_session(
                    session_id, device_id, museum_id, data["exhibit_id"]
                )
            )

    async def _persist_session(
        self,
        session_id: str,
        device_id: str,
        museum_id: str,
        exhibit_id: str | None,
    ):
        """Upsert the session row and append the exhibit to its visit history.

        Idempotent: re-entering the same beacon refreshes last_active but does
        not duplicate the exhibit in exhibits_visited.
        """
        async with self.db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO device_sessions
                    (session_id, device_id, museum_id, exhibits_visited, last_active)
                VALUES (
                    $1, $2, $3,
                    CASE WHEN $4::text IS NULL THEN '{}'::text[] ELSE ARRAY[$4] END,
                    NOW()
                )
                ON CONFLICT (session_id) DO UPDATE SET
                    last_active = NOW(),
                    exhibits_visited = CASE
                        WHEN $4::text IS NULL
                            THEN device_sessions.exhibits_visited
                        WHEN $4 = ANY(device_sessions.exhibits_visited)
                            THEN device_sessions.exhibits_visited
                        ELSE array_append(device_sessions.exhibits_visited, $4)
                    END
                """,
                session_id,
                device_id,
                museum_id,
                exhibit_id,
            )

    def _schedule_db(self, coro):
        """Run a DB coroutine on the event loop from the paho callback thread,
        logging (rather than swallowing) any failure."""
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        future.add_done_callback(self._log_future_error)

    @staticmethod
    def _log_future_error(fut):
        try:
            fut.result()
        except Exception as e:
            logger.error(f"Session persistence failed: {e}")

    def _publish_retained(self, museum_id: str, beacon_uuid: str, data: dict):
        """Publish beacon content as a retained MQTT message so devices
        get it instantly on subscribe, without waiting for a beacon event."""
        topic = f"museum/{museum_id}/beacon/{beacon_uuid}/content"
        self.publish(topic, data, qos=1, retain=True)


async def main():
    logger.info("Starting Beacon Content Service")
    db_pool = await asyncpg.create_pool(POSTGRES_DSN, min_size=2, max_size=5)

    service = BeaconContentService(db_pool)

    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, service.run)
    await asyncio.sleep(float("inf"))


if __name__ == "__main__":
    asyncio.run(main())

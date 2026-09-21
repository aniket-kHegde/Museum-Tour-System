"""
device/offline_cache.py

SQLite-backed cache for beacon content.
Used as fallback when MQTT/network is unavailable.
Stores the last N exhibit TTS scripts keyed by beacon UUID.
"""

import sqlite3
import time
from pathlib import Path

from loguru import logger


class OfflineCache:
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS exhibit_cache (
        beacon_uuid TEXT PRIMARY KEY,
        exhibit_id  TEXT NOT NULL,
        title       TEXT NOT NULL,
        tts_script  TEXT NOT NULL,
        cached_at   INTEGER NOT NULL
    );
    """
    MAX_AGE_DAYS = 7
    MAX_AGE_SEC = MAX_AGE_DAYS * 86400

    def __init__(self, db_path: str, max_exhibits: int = 50):
        self.db_path = db_path
        self.max_exhibits = max_exhibits
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with self._conn() as conn:
            conn.execute(self.SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def put(self, beacon_uuid: str, exhibit_id: str, title: str, tts_script: str):
        """Cache beacon content. Evicts oldest entry when over limit."""
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO exhibit_cache
                    (beacon_uuid, exhibit_id, title, tts_script, cached_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (beacon_uuid, exhibit_id, title, tts_script, int(time.time())),
            )
            # Evict oldest if over max
            count = conn.execute("SELECT COUNT(*) FROM exhibit_cache").fetchone()[0]
            if count > self.max_exhibits:
                conn.execute(
                    """
                    DELETE FROM exhibit_cache WHERE beacon_uuid IN (
                        SELECT beacon_uuid FROM exhibit_cache
                        ORDER BY cached_at ASC LIMIT ?
                    )
                    """,
                    (count - self.max_exhibits,),
                )
        logger.debug(f"Cache PUT: {beacon_uuid} → {title}")

    def get(self, beacon_uuid: str) -> dict | None:
        """Retrieve cached content for a beacon UUID, if not expired."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM exhibit_cache WHERE beacon_uuid = ?", (beacon_uuid,)
            ).fetchone()

        if not row:
            return None

        age = int(time.time()) - row["cached_at"]
        if age > self.MAX_AGE_SEC:
            logger.debug(f"Cache EXPIRED for {beacon_uuid} (age {age}s)")
            return None

        logger.debug(f"Cache HIT: {beacon_uuid} → {row['title']}")
        return dict(row)

    def evict_expired(self):
        cutoff = int(time.time()) - self.MAX_AGE_SEC
        with self._conn() as conn:
            deleted = conn.execute(
                "DELETE FROM exhibit_cache WHERE cached_at < ?", (cutoff,)
            ).rowcount
        if deleted:
            logger.info(f"Evicted {deleted} expired cache entries")

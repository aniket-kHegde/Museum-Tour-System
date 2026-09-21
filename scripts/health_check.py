#!/usr/bin/env python3
"""
scripts/health_check.py

Verifies that all infrastructure and services are running correctly.
Tests:
  - MQTT broker reachability
  - PostgreSQL connectivity + exhibits loaded
  - Redis connectivity
  - Qdrant connectivity + collection exists
  - Simulates a beacon event end-to-end

Usage:
    python scripts/health_check.py
    python scripts/health_check.py --museum-id demo-library --verbose
"""

import argparse
import asyncio
import json
import os
import sys
import time
import threading

import asyncpg
import paho.mqtt.client as mqtt
import redis
from dotenv import load_dotenv
from loguru import logger
from qdrant_client import QdrantClient

load_dotenv()

POSTGRES_DSN = (
    f"postgresql://{os.getenv('POSTGRES_USER', 'museum')}"
    f":{os.getenv('POSTGRES_PASSWORD', '')}"
    f"@{os.getenv('POSTGRES_HOST', 'localhost')}"
    f":{os.getenv('POSTGRES_PORT', '5432')}"
    f"/{os.getenv('POSTGRES_DB', 'museum_tour')}"
)

OK = "  ✓"
FAIL = "  ✗"
WARN = "  !"


def check_mqtt(host, port) -> bool:
    result = {"connected": False}
    event = threading.Event()

    def on_connect(client, userdata, flags, rc, props=None):
        result["connected"] = rc == 0
        event.set()

    client = mqtt.Client(protocol=mqtt.MQTTv5)
    username = os.getenv("MQTT_SERVICE_USERNAME", "service_account")
    password = os.getenv("MQTT_SERVICE_PASSWORD", "")
    client.username_pw_set(username, password)
    client.on_connect = on_connect
    try:
        client.connect(host, port, keepalive=5)
        client.loop_start()
        event.wait(timeout=5)
        client.loop_stop()
        client.disconnect()
    except Exception as e:
        print(f"{FAIL} MQTT broker ({host}:{port}): {e}")
        return False

    if result["connected"]:
        print(f"{OK} MQTT broker ({host}:{port}): reachable")
        return True
    else:
        print(f"{FAIL} MQTT broker ({host}:{port}): connection refused")
        return False


async def check_postgres(museum_id: str) -> bool:
    try:
        conn = await asyncpg.connect(POSTGRES_DSN)
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM exhibits WHERE museum_id = $1", museum_id
        )
        beacon_count = await conn.fetchval(
            "SELECT COUNT(*) FROM beacons WHERE museum_id = $1", museum_id
        )
        await conn.close()
        print(f"{OK} PostgreSQL: connected — {count} exhibits, {beacon_count} beacons for '{museum_id}'")
        if count == 0:
            print(f"{WARN}   No exhibits found. Run: python scripts/ingest_exhibits.py")
        return True
    except Exception as e:
        print(f"{FAIL} PostgreSQL: {e}")
        return False


def check_redis() -> bool:
    try:
        r = redis.Redis(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            password=os.getenv("REDIS_PASSWORD") or None,
            decode_responses=True,
            socket_timeout=3,
        )
        r.ping()
        print(f"{OK} Redis: connected")
        return True
    except Exception as e:
        print(f"{FAIL} Redis: {e}")
        return False


def check_qdrant(museum_id: str) -> bool:
    try:
        client = QdrantClient(
            host=os.getenv("QDRANT_HOST", "localhost"),
            port=int(os.getenv("QDRANT_PORT", "6333")),
            timeout=5,
        )
        collections = [c.name for c in client.get_collections().collections]
        cname = f"{museum_id}_knowledge"
        if cname in collections:
            info = client.get_collection(cname)
            count = info.points_count
            print(f"{OK} Qdrant: connected — collection '{cname}' has {count} vectors")
            if count == 0:
                print(f"{WARN}   Empty collection. Run: python scripts/ingest_exhibits.py")
        else:
            print(f"{FAIL} Qdrant: collection '{cname}' not found. Run: python scripts/ingest_exhibits.py")
            return False
        return True
    except Exception as e:
        print(f"{FAIL} Qdrant: {e}")
        return False


def check_e2e_beacon(host, port, museum_id: str) -> bool:
    """Send a simulated beacon event and wait for a TTS response."""
    TEST_DEVICE = "health-check-device"
    TEST_BEACON = "550e8400-e29b-41d4-a716-446655440001"  # Annihilation of Caste
    response_received = threading.Event()
    response_text = {"text": None}

    def on_connect(client, userdata, flags, rc, props=None):
        if rc == 0:
            client.subscribe(f"museum/{museum_id}/device/{TEST_DEVICE}/response", qos=1)
            payload = json.dumps({
                "event": "beacon_enter",
                "beacon_uuid": TEST_BEACON,
                "rssi": -60,
                "device_id": TEST_DEVICE,
                "museum_id": museum_id,
                "session_id": "health-check-session",
                "timestamp": "2024-01-01T00:00:00Z",
            })
            client.publish(
                f"museum/{museum_id}/device/{TEST_DEVICE}/cmd",
                payload,
                qos=1,
            )

    def on_message(client, userdata, msg):
        try:
            data = json.loads(msg.payload)
            response_text["text"] = data.get("text", "")
            response_received.set()
        except Exception:
            pass

    client = mqtt.Client(protocol=mqtt.MQTTv5)
    username = os.getenv("MQTT_SERVICE_USERNAME", "service_account")
    password = os.getenv("MQTT_SERVICE_PASSWORD", "")
    client.username_pw_set(username, password)
    client.on_connect = on_connect
    client.on_message = on_message

    try:
        client.connect(host, port, keepalive=10)
        client.loop_start()
        got_response = response_received.wait(timeout=8)
        client.loop_stop()
        client.disconnect()
    except Exception as e:
        print(f"{FAIL} E2E beacon test: connection error: {e}")
        return False

    if got_response:
        snippet = (response_text["text"] or "")[:80]
        print(f"{OK} E2E beacon test: got TTS response — '{snippet}...'")
        return True
    else:
        print(f"{FAIL} E2E beacon test: no response after 8s")
        print(f"     (Are beacon_service, postgres, and redis all running?)")
        return False


async def main(museum_id: str, run_e2e: bool):
    broker_host = os.getenv("MQTT_BROKER_HOST", "localhost")
    broker_port = int(os.getenv("MQTT_BROKER_PORT", "1883"))

    print(f"\nMuseum Tour System — Health Check")
    print(f"Museum: {museum_id}\n")

    results = []

    print("Infrastructure:")
    results.append(check_mqtt(broker_host, broker_port))
    results.append(await check_postgres(museum_id))
    results.append(check_redis())
    results.append(check_qdrant(museum_id))

    if run_e2e:
        print("\nEnd-to-end test (requires beacon_service running):")
        results.append(check_e2e_beacon(broker_host, broker_port, museum_id))

    passed = sum(results)
    total = len(results)
    print(f"\n{'─'*40}")
    print(f"  {passed}/{total} checks passed")
    if passed == total:
        print("  System is healthy ✓")
    else:
        print("  Some checks failed — see above for details")
    print(f"{'─'*40}\n")

    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--museum-id", default="demo-library")
    parser.add_argument("--e2e", action="store_true", help="Run end-to-end beacon test")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if not args.verbose:
        logger.remove()

    asyncio.run(main(args.museum_id, args.e2e))

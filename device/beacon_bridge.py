"""
device/beacon_bridge.py  —  runs on the LAPTOP, not the Pi.

The Pi has no BLE controller ("No default controller available"), so instead
of scanning on the Pi we scan on a laptop sitting next to it and publish the
beacon_enter event to the broker *impersonating the Pi*.

The backend routes its response by the device_id inside the payload
(services/shared/mqtt_subscriber.py), so the reply lands on the Pi's
.../response topic and the Pi narrates exactly as if it had seen the beacon
itself. No changes are needed on the Pi or in the services.

    # laptop == the broker host (default):
    .venv/bin/python device/beacon_bridge.py

    # separate laptop, broker elsewhere:
    .venv/bin/python device/beacon_bridge.py --broker 10.177.x.x --device-id pi-dev-001

Requires Bluetooth on the laptop (and, on macOS, Bluetooth permission for the
terminal app — same as test_beacon.py).
"""

import argparse
import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paho.mqtt.client as mqtt
from loguru import logger

from config import load_config
from ble_scanner import BLEScanner

# Only bridge beacons we actually have content for. Mirrors the seeded
# `beacons` table (scripts/simulate_device.py). Any other beacon nRF Connect /
# nearby phones advertise is ignored, so the Pi never says "I don't have info
# about this item". Override on the CLI with --uuid (repeatable).
KNOWN_UUIDS = {
    "550e8400-e29b-41d4-a716-446655440001",  # Annihilation of Caste
    "550e8400-e29b-41d4-a716-446655440002",  # The Buddha and His Dhamma
    "550e8400-e29b-41d4-a716-446655440003",  # The Constitution of India
    "550e8400-e29b-41d4-a716-446655440004",  # Deekshabhoomi, Nagpur
    "550e8400-e29b-41d4-a716-446655440005",  # Chaitya Bhoomi, Mumbai
}


class _BridgePublisher:
    """Minimal stand-in for DeviceMQTTClient: BLEScanner only calls
    publish_beacon_enter(). Publishes on the *target device's* cmd topic so the
    backend treats the event as coming from the Pi."""

    def __init__(self, client, museum_id, device_id, session_id):
        self._client = client
        self.museum_id = museum_id
        self.device_id = device_id
        self.session_id = session_id

    def publish_beacon_enter(self, beacon_uuid: str, rssi: int):
        topic = f"museum/{self.museum_id}/device/{self.device_id}/cmd"
        payload = {
            "device_id": self.device_id,
            "museum_id": self.museum_id,
            "session_id": self.session_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": "beacon_enter",
            "beacon_uuid": beacon_uuid,
            "rssi": rssi,
        }
        self._client.publish(topic, json.dumps(payload), qos=1)
        logger.info(f"→ beacon_enter {beacon_uuid} (rssi {rssi}) as {self.device_id}")


class _BridgeSession:
    """Stand-in for SessionState: BLEScanner only calls leave_exhibit()."""

    def __init__(self):
        self.session_id = f"bridge-{uuid.uuid4().hex[:10]}"

    def leave_exhibit(self):
        logger.info("Beacon out of range")


async def run(args):
    cfg = load_config()

    client = mqtt.Client(
        client_id=f"beacon-bridge-{os.getpid()}", protocol=mqtt.MQTTv5
    )
    client.reconnect_delay_set(min_delay=2, max_delay=30)
    # If the broker has password auth on (e.g. the cloud VM), authenticate as the
    # same device user we impersonate. Sent only when a password is provided
    # (MQTT_DEVICE_PASSWORD env, or device_password in device_config.yaml); an
    # anonymous local broker ignores it.
    device_password = os.getenv("MQTT_DEVICE_PASSWORD", cfg.mqtt.device_password)
    if device_password:
        client.username_pw_set(f"device-{args.device_id}", device_password)
    client.connect(args.broker, args.port, keepalive=60)
    client.loop_start()
    logger.info(f"Bridge connected to broker {args.broker}:{args.port}")
    logger.info(
        f"Impersonating device '{args.device_id}' in museum '{args.museum_id}'"
    )

    allowed = set(args.uuid) if args.uuid else KNOWN_UUIDS
    logger.info(f"Allow-list: {len(allowed)} UUID(s); ignoring all others")

    session = _BridgeSession()
    pub = _BridgePublisher(client, args.museum_id, args.device_id, session.session_id)
    ble = BLEScanner(cfg.ble, pub, session, allowed_uuids=allowed, sticky=True)

    if not await ble.adapter_available():
        logger.error(
            "No usable BLE adapter on this laptop. Turn Bluetooth on "
            "(and grant the terminal Bluetooth permission on macOS)."
        )
        return

    await ble.scan_loop()


def main():
    cfg = load_config()
    ap = argparse.ArgumentParser(description="BLE→MQTT beacon bridge (laptop side)")
    ap.add_argument("--broker", default=cfg.mqtt.broker_host,
                    help="MQTT broker host (default: from device_config.yaml)")
    ap.add_argument("--port", type=int, default=cfg.mqtt.broker_port)
    ap.add_argument("--device-id", default=cfg.device_id,
                    help="Pi to impersonate (must match the Pi's device_id)")
    ap.add_argument("--museum-id", default=cfg.museum_id)
    ap.add_argument("--uuid", action="append", metavar="UUID",
                    help="Allow only this beacon UUID (repeatable). "
                         "Default: the seeded exhibit UUIDs.")
    args = ap.parse_args()

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        logger.info("Bridge stopped")


if __name__ == "__main__":
    main()

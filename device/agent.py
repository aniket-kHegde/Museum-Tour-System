"""
device/agent.py

Main entry point for the Museum Tour Device Agent.
Runs on each Raspberry Pi at boot via systemd.

Starts three concurrent tasks:
  1. BLE scanner — detects nearby beacons
  2. Mic handler — listens for voice queries
  3. MQTT loop   — network I/O

Usage:
    python agent.py
    python agent.py --config /path/to/device_config.yaml
"""

import asyncio
import argparse
import os
import signal
import sys

from loguru import logger

from config import load_config
from mqtt_client import DeviceMQTTClient
from ble_scanner import BLEScanner
from mic_handler import MicHandler
from camera_handler import CameraHandler
from tts_handler import TTSHandler
from session import SessionState
from offline_cache import OfflineCache


def setup_logging(level: str = "INFO"):
    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
    )
    logger.add(
        "/tmp/museum_agent.log",
        rotation="10 MB",
        retention="7 days",
        level="DEBUG",
    )


# ── Simulated beacon ──────────────────────────────────────────────────────────
# The onboard BLE chip on this Pi is non-functional, so we can't detect real
# beacons. Until a USB Bluetooth dongle is fitted, publish one beacon_enter for
# the first exhibit in software so the rest of the pipeline (narration, Q&A,
# vision) still works. Remove this once BLE scanning is restored.
SIMULATED_BEACON_UUID = "550e8400-e29b-41d4-a716-446655440001"  # Annihilation of Caste


async def simulate_beacon_hit(mqtt, delay: float = 3.0):
    """Stand-in for BLEScanner: fire a single beacon_enter once MQTT is up."""
    await asyncio.sleep(delay)  # give the MQTT loop time to connect
    logger.info(f"[SIM] Publishing beacon_enter for {SIMULATED_BEACON_UUID}")
    mqtt.publish_beacon_enter(SIMULATED_BEACON_UUID, rssi=-62)


async def beacon_task(ble, mqtt):
    """Choose where beacon_enter events come from. Controlled by the
    BEACON_SOURCE env var:

        auto     (default) real BLE if an adapter is present, else simulated
        ble      force real BLE scanning
        sim      force the one-shot simulated beacon
        external none — events come from an off-device bridge
                 (device/beacon_bridge.py on a nearby laptop) publishing on
                 this device's cmd topic. Use this on a Pi with no BLE.
    """
    source = os.getenv("BEACON_SOURCE", "auto").lower()

    if source == "external":
        logger.info("BEACON_SOURCE=external — beacons provided by an off-device bridge")
        return
    if source == "sim":
        await simulate_beacon_hit(mqtt)
        return
    if source == "ble":
        await ble.scan_loop()
        return

    # auto
    if await ble.adapter_available():
        logger.info("BLE adapter detected — using real beacon scanning")
        await ble.scan_loop()
    else:
        logger.warning("No BLE adapter — falling back to simulated beacon")
        await simulate_beacon_hit(mqtt)


async def main(config_path: str | None = None):
    cfg = load_config()
    setup_logging()

    logger.info("=" * 50)
    logger.info(f"Museum Tour Agent starting")
    logger.info(f"Device ID : {cfg.device_id}")
    logger.info(f"Museum ID : {cfg.museum_id}")
    logger.info(f"Broker    : {cfg.mqtt.broker_host}:{cfg.mqtt.broker_port}")
    logger.info("=" * 50)

    # Initialise components
    cache = OfflineCache(cfg.offline_cache.db_path, cfg.offline_cache.max_exhibits)
    session = SessionState(cfg.device_id, cfg.museum_id)
    tts = TTSHandler(cfg.tts)

    mqtt = DeviceMQTTClient(cfg, session, tts, cache)
    camera = CameraHandler(cfg.camera, mqtt, session)
    mic = MicHandler(cfg.audio, mqtt, session, camera, cfg.buttons)
    ble = BLEScanner(cfg.ble, mqtt, session)

    # Greeting on startup
    tts.speak(
        f"Welcome to the tour. Walk up to any exhibit to hear about it, "
        f"or ask me a question at any time."
    )

    # Connect MQTT
    await mqtt.connect()

    # Graceful shutdown on SIGINT / SIGTERM
    loop = asyncio.get_event_loop()

    def _shutdown(sig, frame):
        logger.info(f"Received {sig.name}, shutting down...")
        for task in asyncio.all_tasks(loop):
            task.cancel()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Run all tasks concurrently
    try:
        await asyncio.gather(
            beacon_task(ble, mqtt),      # real BLE if adapter present, else simulated beacon
            mic.listen_loop(),           # mic (push-to-talk: SPACE/mic-button=ask, C/camera-button=photo)
            mqtt.loop_forever(),
        )
    except asyncio.CancelledError:
        logger.info("Agent stopped cleanly")
    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Museum Tour Device Agent")
    parser.add_argument("--config", help="Path to device_config.yaml")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    asyncio.run(main(args.config))

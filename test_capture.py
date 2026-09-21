"""
test_capture.py — manual test for the camera + MQTT publish paths.

Runs without the mic or BLE. It:
  1. Captures a photo from the Pi camera and saves it to /tmp/test_capture.jpg
  2. Publishes a beacon_enter event (cmd topic)
  3. Publishes the captured photo (cam topic)

Run on the Pi from inside the project folder:
    python test_capture.py
"""

import asyncio
import base64
import time

from config import load_config
from session import SessionState
from offline_cache import OfflineCache
from tts_handler import TTSHandler
from mqtt_client import DeviceMQTTClient
from camera_handler import CameraHandler


def main():
    cfg = load_config()
    session = SessionState(cfg.device_id, cfg.museum_id)
    tts = TTSHandler(cfg.tts)
    cache = OfflineCache(cfg.offline_cache.db_path, cfg.offline_cache.max_exhibits)

    mqtt = DeviceMQTTClient(cfg, session, tts, cache)
    camera = CameraHandler(cfg.camera, mqtt, session)

    # Connect and run the MQTT network loop in the background
    asyncio.run(mqtt.connect())
    mqtt._client.loop_start()
    time.sleep(1.0)  # allow connect + subscribe

    # --- Test A: beacon event (cmd publish path) ---
    mqtt.publish_beacon_enter("550e8400-e29b-41d4-a716-446655440001", rssi=-55)
    print(">> beacon_enter published")

    # --- Test B: capture a photo, save it locally, then publish it ---
    b64 = camera._capture()
    with open("/tmp/test_capture.jpg", "wb") as f:
        f.write(base64.b64decode(b64))
    print(f">> saved /tmp/test_capture.jpg ({len(b64)} b64 chars)")

    camera.capture_and_publish()  # cam publish path

    time.sleep(2.0)  # flush outbound messages
    mqtt._client.loop_stop()
    print(">> done")


if __name__ == "__main__":
    main()

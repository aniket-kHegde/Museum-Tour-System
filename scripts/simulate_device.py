#!/usr/bin/env python3
"""
scripts/simulate_device.py

Simulates a Raspberry Pi device for local testing WITHOUT real hardware.
Lets you manually trigger:
  - Beacon proximity events (choose an exhibit from the list)
  - Voice queries (type a question)
  - Camera captures (uses a test image or generates a placeholder)

Usage:
    python scripts/simulate_device.py
    python scripts/simulate_device.py --device-id pi-test-001 --museum-id demo-library
"""

import argparse
import base64
import io
import json
import os
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import paho.mqtt.client as mqtt
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

BROKER_HOST = os.getenv("MQTT_BROKER_HOST", "localhost")
BROKER_PORT = int(os.getenv("MQTT_BROKER_PORT", "1883"))
USERNAME = os.getenv("MQTT_SERVICE_USERNAME", "service_account")
PASSWORD = os.getenv("MQTT_SERVICE_PASSWORD", "")

def load_exhibits() -> list[dict]:
    """Read the beacon-backed exhibits straight from the seed file.

    This used to be a hardcoded copy of the seed data, which silently drifted
    whenever the content changed. Archive entries (no beacon_uuid) are skipped —
    there is nothing to walk up to.
    """
    path = Path(__file__).parent.parent / "data" / "exhibits" / "seed_exhibits.json"
    try:
        entries = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"  ! Could not read {path}: {e}")
        return []
    return [
        {"id": e["id"], "uuid": e["beacon_uuid"], "title": e["title"]}
        for e in entries
        if e.get("beacon_uuid")
    ]


def make_placeholder_image() -> str:
    """Generate a tiny valid JPEG as a placeholder camera capture."""
    try:
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (320, 240), color=(210, 180, 140))
        draw = ImageDraw.Draw(img)
        draw.rectangle([20, 20, 300, 220], outline=(100, 70, 40), width=4)
        draw.text((100, 100), "TEST PHOTO", fill=(80, 50, 20))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=75)
        return base64.b64encode(buf.getvalue()).decode("utf-8")
    except ImportError:
        # Fallback: a 1x1 white JPEG (valid but tiny)
        return "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAABAAEDASIAAhEBAxEB/8QAFAABAAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/xAAUAQEAAAAAAAAAAAAAAAAAAAAA/8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAwDAQACEQMRAD8AJQAB/9k="


class DeviceSimulator:
    def __init__(self, device_id: str, museum_id: str):
        self.device_id = device_id
        self.museum_id = museum_id
        self.session_id = f"sess-{uuid.uuid4().hex[:8]}"
        self.current_exhibit = None

        self.client = mqtt.Client(client_id=f"simulator-{device_id}", protocol=mqtt.MQTTv5)
        self.client.username_pw_set(USERNAME, PASSWORD)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            print(f"  [mqtt] Connected to broker at {BROKER_HOST}:{BROKER_PORT}")
            # Subscribe to our device's response topic
            topic = f"museum/{self.museum_id}/device/{self.device_id}/response"
            client.subscribe(topic, qos=1)
            print(f"  [mqtt] Listening for responses on: {topic}")
        else:
            print(f"  [mqtt] Connection failed, rc={rc}")

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
        except Exception:
            return

        rtype = payload.get("response_type", "?")
        text = payload.get("text", "")

        print(f"\n{'='*60}")
        print(f"  RESPONSE [{rtype.upper()}]")
        print(f"{'='*60}")
        print(f"  {text}")
        print(f"{'='*60}\n")

        # In a real device, this would call TTSHandler.speak(text)
        # For simulation, we just print it.

    def connect(self):
        self.client.connect(BROKER_HOST, BROKER_PORT, keepalive=60)
        t = threading.Thread(target=self.client.loop_forever, daemon=True)
        t.start()
        time.sleep(0.5)  # let connect settle

    def publish(self, subtopic: str, payload: dict):
        topic = f"museum/{self.museum_id}/device/{self.device_id}/{subtopic}"
        payload.update({
            "device_id": self.device_id,
            "museum_id": self.museum_id,
            "session_id": self.session_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        self.client.publish(topic, json.dumps(payload), qos=1)
        print(f"  [mqtt] Published to {topic}")

    def trigger_beacon(self, exhibit: dict):
        self.current_exhibit = exhibit
        self.publish("cmd", {
            "event": "beacon_enter",
            "beacon_uuid": exhibit["uuid"],
            "rssi": -62,
        })
        print(f"  > Beacon triggered for: {exhibit['title']}")

    def ask_question(self, question: str):
        if not self.current_exhibit:
            print("  ! Walk up to a beacon first (option 1)")
            return
        self.publish("voice", {
            "event": "voice_query",
            "transcript": question,
            "exhibit_id": self.current_exhibit["id"],
        })
        print(f"  > Voice query sent: '{question}'")

    def take_photo(self, image_path: str | None = None):
        if image_path and os.path.exists(image_path):
            with open(image_path, "rb") as f:
                image_b64 = base64.b64encode(f.read()).decode()
        else:
            print("  (using placeholder test image)")
            image_b64 = make_placeholder_image()

        self.publish("cam", {
            "event": "camera_capture",
            "image_b64": image_b64,
            "exhibit_id": self.current_exhibit["id"] if self.current_exhibit else None,
        })
        print("  > Camera capture sent")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device-id", default="pi-sim-001")
    parser.add_argument("--museum-id", default="demo-library")
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print(f"  Ambedkar Memorial Museum — Device Simulator")
    print(f"  Device: {args.device_id}  |  Museum: {args.museum_id}")
    print(f"{'='*60}\n")

    exhibits = load_exhibits()
    if not exhibits:
        print("  ! No beacon-backed exhibits found. Run scripts/ingest_exhibits.py first.")
        return

    sim = DeviceSimulator(args.device_id, args.museum_id)
    sim.connect()

    while True:
        print("\nWhat would you like to do?")
        print("  1. Walk up to an exhibit beacon")
        print("  2. Ask a question (voice query)")
        print("  3. Take a photo")
        print("  4. Exit")
        choice = input("\n> ").strip()

        if choice == "1":
            print("\nAvailable exhibits:")
            for i, exhibit in enumerate(exhibits, 1):
                print(f"  {i}. {exhibit['title']}")
            try:
                idx = int(input("Choose exhibit number: ")) - 1
                if idx < 0:
                    raise IndexError
                sim.trigger_beacon(exhibits[idx])
                time.sleep(0.2)
            except (ValueError, IndexError):
                print("  Invalid choice")

        elif choice == "2":
            question = input("Your question: ").strip()
            if question:
                sim.ask_question(question)
                time.sleep(0.2)

        elif choice == "3":
            path = input("Path to image file (leave blank for test image): ").strip()
            sim.take_photo(path or None)
            time.sleep(0.2)

        elif choice == "4":
            print("Goodbye!")
            break
        else:
            print("  Invalid choice, try again")


if __name__ == "__main__":
    main()

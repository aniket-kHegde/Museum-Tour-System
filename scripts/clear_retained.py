#!/usr/bin/env python3
"""
scripts/clear_retained.py

Clears retained `museum/{museum_id}/beacon/{uuid}/content` messages from the broker.

beacon_service publishes each exhibit's narration to a retained topic so a device
that joins later gets it immediately, and `device/mqtt_client.py` caches those
payloads for offline narration. Retained messages outlive the process that sent
them and are not touched by re-ingesting, so after a content swap the broker
still hands out the PREVIOUS subject's titles and scripts — a Pi with no network
would narrate content that is no longer in the database.

Run this once after changing the seed content. beacon_service republishes the
correct payload the next time each beacon fires.

Usage:
    python scripts/clear_retained.py                  # list what is retained
    python scripts/clear_retained.py --clear          # delete it
    python scripts/clear_retained.py --clear --museum-id demo-library
"""

import argparse
import json
import os
import time

import paho.mqtt.client as mqtt
from dotenv import load_dotenv

load_dotenv()

BROKER_HOST = os.getenv("MQTT_BROKER_HOST", "localhost")
BROKER_PORT = int(os.getenv("MQTT_BROKER_PORT", "1883"))
USERNAME = os.getenv("MQTT_SERVICE_USERNAME", "service_account")
PASSWORD = os.getenv("MQTT_SERVICE_PASSWORD", "")


def main(museum_id: str, do_clear: bool, wait: float):
    pattern = f"museum/{museum_id}/beacon/+/content"
    found: dict[str, str] = {}

    def on_connect(client, userdata, flags, rc, properties=None):
        if rc != 0:
            print(f"  ! MQTT connect failed, rc={rc}")
            return
        client.subscribe(pattern, qos=1)

    def on_message(client, userdata, msg):
        if not msg.payload:
            return  # already-cleared tombstone
        try:
            found[msg.topic] = json.loads(msg.payload.decode()).get("title", "?")
        except Exception:  # noqa: BLE001 - show it regardless of shape
            found[msg.topic] = f"<{len(msg.payload)} bytes>"

    client = mqtt.Client(client_id=f"clear-retained-{os.getpid()}", protocol=mqtt.MQTTv5)
    client.username_pw_set(USERNAME, PASSWORD)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(BROKER_HOST, BROKER_PORT, keepalive=30)
    client.loop_start()
    time.sleep(wait)

    if not found:
        print(f"No retained messages under {pattern}")
    else:
        print(f"{len(found)} retained message(s) under {pattern}:")
        for topic in sorted(found):
            print(f"  {topic}\n      title: {found[topic]}")

        if do_clear:
            print()
            for topic in sorted(found):
                # An empty retained payload is the MQTT way to delete one.
                client.publish(topic, payload=b"", qos=1, retain=True)
                print(f"  cleared {topic}")
            time.sleep(1.0)
            print(f"\nCleared {len(found)}. beacon_service will republish on the next beacon.")
        else:
            print("\nRe-run with --clear to delete these.")

    client.loop_stop()
    client.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="List or clear retained beacon content")
    parser.add_argument("--museum-id", default="demo-library")
    parser.add_argument("--clear", action="store_true", help="Delete the retained messages")
    parser.add_argument("--wait", type=float, default=2.0, help="Seconds to collect retained messages")
    args = parser.parse_args()
    main(args.museum_id, args.clear, args.wait)

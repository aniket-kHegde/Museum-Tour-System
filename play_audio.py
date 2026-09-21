"""
play_audio.py — listen to mic clips after they cross MQTT, to check quality.

Subscribes to the device audio topic, decodes each base64 WAV payload back to
a file, prints its size/format, and plays it. Run this in a separate terminal
while the agent is running; every push-to-talk clip will play back here.

Plays via `aplay` on Linux (Pi) or `afplay` on macOS. Saved copies land in
the system temp dir (/tmp/recv_N.wav) so you can inspect or scp them too.

Usage:
    python play_audio.py                       # broker from MQTT_BROKER_HOST or default
    MQTT_BROKER_HOST=localhost python play_audio.py
"""

import base64
import json
import os
import subprocess
import sys
import tempfile

import paho.mqtt.client as mqtt

BROKER = os.getenv("MQTT_BROKER_HOST", "localhost")
PORT = int(os.getenv("MQTT_BROKER_PORT", "1883"))
TOPIC = "museum/+/device/+/audio"

PLAYER = ["afplay"] if sys.platform == "darwin" else ["aplay", "-q"]

_count = 0


def on_connect(client, userdata, flags, rc, properties=None):
    print(f">> connected (rc={rc}); subscribing to {TOPIC}")
    client.subscribe(TOPIC, qos=1)


def on_message(client, userdata, msg):
    global _count
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
    except Exception as e:
        print(f"!! bad payload on {msg.topic}: {e}")
        return

    audio_b64 = payload.get("audio_b64")
    if not audio_b64:
        return

    _count += 1
    data = base64.b64decode(audio_b64)
    path = os.path.join(tempfile.gettempdir(), f"recv_{_count}.wav")
    with open(path, "wb") as f:
        f.write(data)

    mime = payload.get("mime_type", "audio/wav")
    device = payload.get("device_id", "?")
    print(f"[{_count}] {len(data)} bytes ({mime}) from {device} -> {path}  ▶ playing")
    try:
        subprocess.run(PLAYER + [path], check=False)
    except FileNotFoundError:
        print(f"!! '{PLAYER[0]}' not found — play it manually: {path}")


def main():
    client = mqtt.Client(client_id="audio-listener", protocol=mqtt.MQTTv5)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(BROKER, PORT, 60)
    print(f">> listening on {BROKER}:{PORT} for audio clips... (Ctrl+C to quit)")
    client.loop_forever()


if __name__ == "__main__":
    main()

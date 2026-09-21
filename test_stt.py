"""
test_stt.py — test the server-side speech-to-text path.

Publishes a WAV audio clip to the device's /audio topic, exactly like the
mic handler would. The QA service transcribes it with Gemini, runs RAG, and
publishes a spoken answer back on the /response topic (the response JSON's
"query" field contains the transcript Gemini produced).

Usage on the Pi (inside the project venv):
    espeak -w /tmp/q.wav "Who wrote the Annihilation of Caste?"
    python test_stt.py /tmp/q.wav
"""

import asyncio
import base64
import sys
import time

from config import load_config
from session import SessionState
from offline_cache import OfflineCache
from tts_handler import TTSHandler
from mqtt_client import DeviceMQTTClient
from camera_handler import CameraHandler  # noqa: F401  (kept for parity, unused)


def main():
    wav_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/q.wav"

    cfg = load_config()
    session = SessionState(cfg.device_id, cfg.museum_id)
    tts = TTSHandler(cfg.tts)
    cache = OfflineCache(cfg.offline_cache.db_path, cfg.offline_cache.max_exhibits)
    mqtt = DeviceMQTTClient(cfg, session, tts, cache)

    with open(wav_path, "rb") as f:
        audio_b64 = base64.b64encode(f.read()).decode("utf-8")
    print(f">> loaded {wav_path} ({len(audio_b64)} b64 chars)")

    asyncio.run(mqtt.connect())
    mqtt._client.loop_start()
    time.sleep(1.0)  # allow connect + subscribe

    mqtt.publish_audio_query(audio_b64, mime_type="audio/wav")
    print(">> audio_query published — waiting for the QA service to respond...")

    # Gemini STT + RAG + LLM can take a few seconds.
    time.sleep(10.0)
    mqtt._client.loop_stop()
    print(">> done")


if __name__ == "__main__":
    main()

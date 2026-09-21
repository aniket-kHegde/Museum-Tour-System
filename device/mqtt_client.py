"""
device/mqtt_client.py

MQTT client wrapper for the Raspberry Pi device.
Handles connection, TLS, subscriptions, reconnection,
and publishing for all three feature topics (cmd, voice, cam).
Incoming responses are dispatched to TTS or logged.
"""

import asyncio
import json
import ssl
import threading
import uuid
from datetime import datetime, timezone

import paho.mqtt.client as mqtt
from loguru import logger


class DeviceMQTTClient:
    def __init__(self, cfg, session, tts_handler, offline_cache):
        self.cfg = cfg
        self.session = session
        self.tts = tts_handler
        self.cache = offline_cache

        self._client = mqtt.Client(
            client_id=f"device-{cfg.device_id}",
            protocol=mqtt.MQTTv5,
        )

        # Auth (for production with EMQX password auth)
        username = f"device-{cfg.device_id}"
        password = getattr(cfg.mqtt, "device_password", "")
        if password:
            self._client.username_pw_set(username, password)

        # TLS (production)
        if cfg.mqtt.use_tls and cfg.mqtt.tls_ca_cert:
            self._client.tls_set(
                ca_certs=cfg.mqtt.tls_ca_cert,
                certfile=cfg.mqtt.tls_client_cert or None,
                keyfile=cfg.mqtt.tls_client_key or None,
                tls_version=ssl.PROTOCOL_TLS_CLIENT,
            )

        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

        # Response topic for this device
        self._response_topic = (
            f"museum/{cfg.museum_id}/device/{cfg.device_id}/response"
        )
        # Retained beacon content topic (subscribe to all in this museum)
        self._beacon_topic = f"museum/{cfg.museum_id}/beacon/+/content"

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def connect(self):
        self._client.reconnect_delay_set(min_delay=2, max_delay=30)
        logger.info(
            f"Connecting to MQTT broker "
            f"{self.cfg.mqtt.broker_host}:{self.cfg.mqtt.broker_port}"
        )
        self._client.connect(
            self.cfg.mqtt.broker_host,
            self.cfg.mqtt.broker_port,
            keepalive=self.cfg.mqtt.keepalive,
        )

    async def loop_forever(self):
        """Run the MQTT network loop in a thread executor.

        Survives transient paho/broker errors (e.g. a PUBACK whose reason code
        paho can't parse — which otherwise raises and would kill the agent) by
        reconnecting and resuming, rather than letting the exception propagate.
        """
        loop = asyncio.get_event_loop()
        while True:
            try:
                await loop.run_in_executor(None, self._client.loop_forever)
                return  # clean shutdown (disconnect called)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"MQTT loop error ({e!r}) — reconnecting in 3s")
                await asyncio.sleep(3)
                try:
                    self._client.reconnect()
                except Exception as re:
                    logger.warning(f"MQTT reconnect failed: {re!r}")

    # ── Callbacks ─────────────────────────────────────────────────────────────

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            logger.info("Device connected to MQTT broker")
            client.subscribe(self._response_topic, qos=1)
            client.subscribe(self._beacon_topic, qos=1)
            logger.info(f"Subscribed to: {self._response_topic}")
            logger.info(f"Subscribed to: {self._beacon_topic}")
        else:
            logger.error(f"MQTT connection failed, rc={rc}")

    def _on_disconnect(self, client, userdata, rc, properties=None):
        if rc != 0:
            logger.warning(f"Disconnected from MQTT (rc={rc}), will auto-reconnect")

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception as e:
            logger.warning(f"Bad MQTT message on {msg.topic}: {e}")
            return

        # Retained beacon content — cache it locally
        if "/beacon/" in msg.topic and msg.retain:
            self._handle_beacon_cache(msg.topic, payload)
            return

        # Response from a backend service
        rtype = payload.get("response_type")
        text = payload.get("text", "")

        if rtype == "tts" and text:
            self.session.is_speaking = True
            self.tts.speak(text)
            self.session.is_speaking = False
        elif rtype == "error":
            logger.warning(f"Service error response: {text}")
            self.tts.speak(
                text or "Sorry, something went wrong. Please try again."
            )
        else:
            logger.debug(f"Unhandled response type: {rtype}")

    def _handle_beacon_cache(self, topic: str, payload: dict):
        """Cache retained beacon content for offline use."""
        # Topic format: museum/{museum_id}/beacon/{uuid}/content
        parts = topic.split("/")
        if len(parts) >= 4:
            beacon_uuid = parts[3]
            exhibit_id = payload.get("exhibit_id", "")
            title = payload.get("title", "")
            tts_script = payload.get("tts_script", "")
            if beacon_uuid and tts_script:
                self.cache.put(beacon_uuid, exhibit_id, title, tts_script)

    # ── Publish helpers ───────────────────────────────────────────────────────

    def _base_payload(self) -> dict:
        return {
            "device_id": self.cfg.device_id,
            "museum_id": self.cfg.museum_id,
            "session_id": self.session.session_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def _publish(self, subtopic: str, payload: dict, qos: int = 1):
        topic = f"museum/{self.cfg.museum_id}/device/{self.cfg.device_id}/{subtopic}"
        full_payload = {**self._base_payload(), **payload}
        result = self._client.publish(topic, json.dumps(full_payload), qos=qos)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            logger.error(f"Publish failed on {topic}: rc={result.rc}")
            return False
        return True

    def publish_beacon_enter(self, beacon_uuid: str, rssi: int):
        """Publish a beacon proximity event. Falls back to offline cache on failure."""
        ok = self._publish("cmd", {
            "event": "beacon_enter",
            "beacon_uuid": beacon_uuid,
            "rssi": rssi,
        })
        if not ok:
            logger.warning("MQTT unavailable — trying offline cache")
            cached = self.cache.get(beacon_uuid)
            if cached:
                self.session.enter_exhibit(
                    beacon_uuid, cached["exhibit_id"], cached["title"]
                )
                self.tts.speak(cached["tts_script"])
            else:
                self.tts.speak(
                    "Welcome! I'm having trouble connecting, "
                    "but I'll try to load information shortly."
                )

    def publish_voice_query(self, transcript: str):
        """Publish a transcribed voice query."""
        if not self.session.has_exhibit:
            # Still useful to ask even without a beacon — search the full museum
            logger.info("Voice query with no active exhibit — full museum search")

        self._publish("voice", {
            "event": "voice_query",
            "transcript": transcript,
            "exhibit_id": self.session.current_exhibit_id,
        })

    def publish_audio_query(self, audio_b64: str, mime_type: str = "audio/wav"):
        """Publish a base64-encoded audio clip for server-side transcription + RAG."""
        if not self.session.has_exhibit:
            logger.info("Audio query with no active exhibit — full museum search")

        self._publish("audio", {
            "event": "audio_query",
            "audio_b64": audio_b64,
            "mime_type": mime_type,
            "exhibit_id": self.session.current_exhibit_id,
        }, qos=1)

    def publish_camera_capture(self, image_b64: str):
        """Publish a base64-encoded JPEG camera capture."""
        self._publish("cam", {
            "event": "camera_capture",
            "image_b64": image_b64,
            "exhibit_id": self.session.current_exhibit_id,
        }, qos=1)

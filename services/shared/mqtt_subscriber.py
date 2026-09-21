"""
services/shared/mqtt_subscriber.py

Base class for all backend microservices.
Handles connect, TLS, subscribe, reconnect, and response publishing.
Each service subclasses this and implements handle_message().
"""

import json
import logging
import os
import ssl
import time
from abc import ABC, abstractmethod
from typing import Any

import paho.mqtt.client as mqtt
from loguru import logger


class MQTTSubscriberService(ABC):
    """
    Base MQTT subscriber for beacon, Q&A, and vision services.

    Subclasses must define:
        TOPICS: list[str]   — topic filters to subscribe to (supports wildcards)

    Subclasses must implement:
        handle_message(topic: str, payload: dict) -> dict | None
            Return a dict to publish as a response to the device, or None to skip.
    """

    TOPICS: list[str] = []

    def __init__(self):
        self.broker_host = os.getenv("MQTT_BROKER_HOST", "localhost")
        self.broker_port = int(os.getenv("MQTT_BROKER_PORT", "1883"))
        self.username = os.getenv("MQTT_SERVICE_USERNAME", "service_account")
        self.password = os.getenv("MQTT_SERVICE_PASSWORD", "")
        self.use_tls = os.getenv("MQTT_USE_TLS", "false").lower() == "true"
        self.ca_cert = os.getenv("MQTT_TLS_CA_CERT", "")

        self.client = mqtt.Client(
            client_id=f"{self.__class__.__name__}-{os.getpid()}",
            protocol=mqtt.MQTTv5,
        )
        self.client.username_pw_set(self.username, self.password)

        if self.use_tls and self.ca_cert:
            self.client.tls_set(
                ca_certs=self.ca_cert,
                tls_version=ssl.PROTOCOL_TLS_CLIENT,
            )

        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    # ── Internal callbacks ────────────────────────────────────────────────────

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            logger.info(f"{self.__class__.__name__} connected to MQTT broker")
            for topic in self.TOPICS:
                client.subscribe(topic, qos=1)
                logger.info(f"  Subscribed: {topic}")
        else:
            logger.error(f"MQTT connect failed, rc={rc}")

    def _on_disconnect(self, client, userdata, rc, properties=None):
        if rc != 0:
            logger.warning(f"Unexpected MQTT disconnect (rc={rc}), reconnecting...")
            # paho auto-reconnects with reconnect_delay_set

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.warning(f"Bad payload on {msg.topic}: {e}")
            return

        logger.debug(f"Received on {msg.topic}: {payload.get('event', '?')}")

        t_recv = time.perf_counter()
        try:
            response = self.handle_message(msg.topic, payload)
        except Exception as e:
            logger.exception(f"handle_message error: {e}")
            response = {
                "response_type": "error",
                "text": "Sorry, something went wrong. Please try again.",
            }

        if response is not None:
            device_id = payload.get("device_id")
            museum_id = payload.get("museum_id")
            if device_id and museum_id:
                response_topic = f"museum/{museum_id}/device/{device_id}/response"
                response["session_id"] = payload.get("session_id")
                # Echo the request id so a benchmark/client can match this
                # response to the request it sent and compute end-to-end latency.
                if payload.get("request_id") is not None:
                    response["request_id"] = payload.get("request_id")
                # Total time spent inside this service (handler + serialization
                # up to publish). Stored under _timings.service_ms alongside any
                # per-stage timings the handler already recorded.
                timings = response.setdefault("_timings", {})
                timings["service_ms"] = round((time.perf_counter() - t_recv) * 1000.0, 2)
                self.publish(response_topic, response)

    # ── Public API ────────────────────────────────────────────────────────────

    def publish(self, topic: str, payload: dict, qos: int = 0, retain: bool = False):
        result = self.client.publish(
            topic,
            json.dumps(payload),
            qos=qos,
            retain=retain,
        )
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            logger.error(f"Publish failed to {topic}: rc={result.rc}")

    def run(self):
        """Start the service. Blocks forever."""
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        logger.info(
            f"Connecting to MQTT broker at {self.broker_host}:{self.broker_port}"
        )
        self.client.connect(self.broker_host, self.broker_port, keepalive=60)
        self.client.loop_forever()

    # ── Abstract ──────────────────────────────────────────────────────────────

    @abstractmethod
    def handle_message(self, topic: str, payload: dict) -> dict | None:
        """
        Process an incoming MQTT message.

        Args:
            topic:   The MQTT topic string.
            payload: Parsed JSON payload dict.

        Returns:
            A dict to publish back to the device's /response topic,
            or None to send no response.
        """
        ...

"""
device/config.py

Loads device configuration from device_config.yaml + .env overrides.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()

CONFIG_PATH = Path(__file__).parent / "device_config.yaml"


@dataclass
class MQTTConfig:
    broker_host: str = "localhost"
    broker_port: int = 1883
    use_tls: bool = False
    tls_ca_cert: str = ""
    tls_client_cert: str = ""
    tls_client_key: str = ""
    keepalive: int = 60
    device_password: str = ""   # broker password for user "device-{device_id}" (set when the broker has password auth on, e.g. the cloud VM)


@dataclass
class BLEConfig:
    scan_interval_ms: int = 500
    rssi_threshold: int = -70
    debounce_seconds: float = 2.0
    beacon_type: str = "iBeacon"


@dataclass
class AudioConfig:
    input_device_index: int = 0
    sample_rate: int = 16000
    transmit_sample_rate: int = 16000  # downsample to this before publishing (keeps base64 WAV under broker limit; STT doesn't need >16k)
    silence_threshold: float = 0.01
    silence_duration_ms: int = 1500
    max_clip_seconds: float = 10.0   # cap so base64 WAV stays under broker payload limit


@dataclass
class CameraConfig:
    resolution: list = field(default_factory=lambda: [1280, 720])
    jpeg_quality: int = 75
    max_payload_kb: int = 150


@dataclass
class ButtonsConfig:
    enabled: bool = True
    mic_pin: int = 17        # BCM GPIO for the "ask" button (mirrors SPACE)
    camera_pin: int = 27     # BCM GPIO for the "photo" button (mirrors C)
    pull_up: bool = True     # True = button wired between GPIO and GND (internal pull-up)
    bounce_time: float = 0.05  # debounce, seconds


@dataclass
class TTSConfig:
    engine: str = "pyttsx3"   # "pyttsx3" or "gtts"
    rate: int = 150
    voice_id: str = None


@dataclass
class OfflineCacheConfig:
    db_path: str = "/tmp/museum_cache.db"
    max_exhibits: int = 50


@dataclass
class DeviceConfig:
    device_id: str = "pi-dev-001"
    museum_id: str = "demo-library"
    mqtt: MQTTConfig = field(default_factory=MQTTConfig)
    ble: BLEConfig = field(default_factory=BLEConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    camera: CameraConfig = field(default_factory=CameraConfig)
    buttons: ButtonsConfig = field(default_factory=ButtonsConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    offline_cache: OfflineCacheConfig = field(default_factory=OfflineCacheConfig)


def load_config() -> DeviceConfig:
    cfg = DeviceConfig()

    # Load from YAML if it exists
    if CONFIG_PATH.exists():
        raw = yaml.safe_load(CONFIG_PATH.read_text())
        cfg.device_id = raw.get("device_id", cfg.device_id)
        cfg.museum_id = raw.get("museum_id", cfg.museum_id)

        if "mqtt" in raw:
            m = raw["mqtt"]
            cfg.mqtt.broker_host = m.get("broker_host", cfg.mqtt.broker_host)
            cfg.mqtt.broker_port = m.get("broker_port", cfg.mqtt.broker_port)
            cfg.mqtt.use_tls = m.get("use_tls", cfg.mqtt.use_tls)
            cfg.mqtt.tls_ca_cert = m.get("tls_ca_cert", cfg.mqtt.tls_ca_cert)
            cfg.mqtt.tls_client_cert = m.get("tls_client_cert", cfg.mqtt.tls_client_cert)
            cfg.mqtt.tls_client_key = m.get("tls_client_key", cfg.mqtt.tls_client_key)
            cfg.mqtt.device_password = m.get("device_password", cfg.mqtt.device_password)

        if "ble" in raw:
            b = raw["ble"]
            cfg.ble.scan_interval_ms = b.get("scan_interval_ms", cfg.ble.scan_interval_ms)
            cfg.ble.rssi_threshold = b.get("rssi_threshold", cfg.ble.rssi_threshold)
            cfg.ble.debounce_seconds = b.get("debounce_seconds", cfg.ble.debounce_seconds)

        if "audio" in raw:
            a = raw["audio"]
            cfg.audio.input_device_index = a.get("input_device_index", cfg.audio.input_device_index)
            cfg.audio.sample_rate = a.get("sample_rate", cfg.audio.sample_rate)
            cfg.audio.transmit_sample_rate = a.get("transmit_sample_rate", cfg.audio.transmit_sample_rate)
            cfg.audio.silence_threshold = a.get("silence_threshold", cfg.audio.silence_threshold)
            cfg.audio.silence_duration_ms = a.get("silence_duration_ms", cfg.audio.silence_duration_ms)
            cfg.audio.max_clip_seconds = a.get("max_clip_seconds", cfg.audio.max_clip_seconds)

        if "camera" in raw:
            c = raw["camera"]
            cfg.camera.resolution = c.get("resolution", cfg.camera.resolution)
            cfg.camera.jpeg_quality = c.get("jpeg_quality", cfg.camera.jpeg_quality)
            cfg.camera.max_payload_kb = c.get("max_payload_kb", cfg.camera.max_payload_kb)

        if "buttons" in raw:
            bt = raw["buttons"]
            cfg.buttons.enabled = bt.get("enabled", cfg.buttons.enabled)
            cfg.buttons.mic_pin = bt.get("mic_pin", cfg.buttons.mic_pin)
            cfg.buttons.camera_pin = bt.get("camera_pin", cfg.buttons.camera_pin)
            cfg.buttons.pull_up = bt.get("pull_up", cfg.buttons.pull_up)
            cfg.buttons.bounce_time = bt.get("bounce_time", cfg.buttons.bounce_time)

        if "tts" in raw:
            t = raw["tts"]
            cfg.tts.engine = t.get("engine", cfg.tts.engine)
            cfg.tts.rate = t.get("rate", cfg.tts.rate)

        if "offline_cache" in raw:
            o = raw["offline_cache"]
            cfg.offline_cache.db_path = o.get("db_path", cfg.offline_cache.db_path)
            cfg.offline_cache.max_exhibits = o.get("max_exhibits", cfg.offline_cache.max_exhibits)

    # Environment variable overrides (useful for Docker / CI)
    cfg.device_id = os.getenv("DEVICE_ID", cfg.device_id)
    cfg.museum_id = os.getenv("MUSEUM_ID", cfg.museum_id)
    cfg.mqtt.broker_host = os.getenv("MQTT_BROKER_HOST", cfg.mqtt.broker_host)
    cfg.mqtt.broker_port = int(os.getenv("MQTT_BROKER_PORT", cfg.mqtt.broker_port))
    cfg.mqtt.device_password = os.getenv("MQTT_DEVICE_PASSWORD", cfg.mqtt.device_password)

    return cfg

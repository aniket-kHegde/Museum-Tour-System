"""
dashboard/app.py

A simple Flask web dashboard that acts as a pure MQTT observer for the museum
tour system. It subscribes to `museum/#`, classifies traffic, and streams it to
the browser over Server-Sent Events.

Shows five things in real time:
  1. Camera images captured on the device   (.../cam)
  2. Generated text (TTS answers)            (.../response)
  3. Transcribed text (what the user said)   (.../voice, and .../response query)
  4. Activated BLE node + RSSI               (.../cmd  event=beacon_enter)
  5. Device position on a venue floor plan   (derived from 4 + seed_exhibits.json)

Read-only: it never publishes and makes no changes to any other service.

Run:
    pip install -r dashboard/requirements.txt
    cd dashboard && python app.py
    # open http://localhost:5005
"""

import base64
import collections
import hashlib
import itertools
import json
import os
import queue
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import paho.mqtt.client as mqtt
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template

# ── Config ──────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

BROKER_HOST = os.getenv("MQTT_BROKER_HOST", "localhost")
BROKER_PORT = int(os.getenv("MQTT_BROKER_PORT", "1883"))
USERNAME = os.getenv("MQTT_SERVICE_USERNAME", "service_account")
PASSWORD = os.getenv("MQTT_SERVICE_PASSWORD", "")
USE_TLS = os.getenv("MQTT_USE_TLS", "false").lower() == "true"
CA_CERT = os.getenv("MQTT_TLS_CA_CERT", "")

SEED_EXHIBITS = REPO_ROOT / "data" / "exhibits" / "seed_exhibits.json"

MAX_EVENTS = 200      # combined feed ring buffer
MAX_IMAGES = 20       # how many JPEGs to keep in memory
MAX_AUDIO = 20        # how many audio clips to keep in memory
PORT = 5005

app = Flask(__name__)

# ── Venue floor plan ────────────────────────────────────────────────────────
#
# Static geometry for the map view. The tour system has no real positioning
# system — the only spatial signal is "which beacon is nearest, and how strong".
# This floor plan gives those beacons a fixed place to live so the browser can
# draw the device moving between them.
#
# Coordinates are in a 1000x640 SVG viewBox. PX_PER_METER sets the scale bar.

PX_PER_METER = 40
SHELVES_PER_SECTION = 3   # "Bay 1".."Bay 3" spread down a gallery's face
ANCHOR_OFFSET = 18        # how far off the gallery face a beacon sits, in px

FLOOR_PLAN = {
    "venue": "Dr. B.R. Ambedkar Memorial Museum",
    "floor": "Ground Floor",
    "width": 1000,
    "height": 640,
    "px_per_meter": PX_PER_METER,
    "walls": {
        "x": 40, "y": 40, "w": 920, "h": 560,
        # Gap in the bottom wall that the entrance path leads through.
        "entrance": {"x": 440, "w": 120},
    },
    "zones": [
        {"id": "A", "kind": "shelf", "label": "Gallery A", "sub": "Writings",
         "x": 100, "y": 100, "w": 220, "h": 140, "face": "right"},
        {"id": "B", "kind": "shelf", "label": "Gallery B", "sub": "Documents & Drafts",
         "x": 680, "y": 100, "w": 220, "h": 140, "face": "left"},
        {"id": "C", "kind": "shelf", "label": "Gallery C", "sub": "Monuments",
         "x": 100, "y": 400, "w": 220, "h": 140, "face": "right"},
        {"id": "D", "kind": "shelf", "label": "Gallery D", "sub": "Memorials & Legacy",
         "x": 680, "y": 400, "w": 220, "h": 140, "face": "left"},
        {"id": "reading", "kind": "room", "label": "Reading Room",
         "x": 400, "y": 260, "w": 200, "h": 120},
        {"id": "desk", "kind": "desk", "label": "Info Desk",
         "x": 430, "y": 496, "w": 140, "h": 46},
    ],
    # Aisle centre-lines the autopilot walks, as a closed loop past every
    # section. The browser also draws these as faint walkway guides.
    "waypoints": [
        [500, 560], [370, 560], [370, 470], [370, 320], [370, 170],
        [500, 170], [630, 170], [630, 320], [630, 470], [500, 470],
    ],
    "entrance_path": [[500, 612], [500, 560]],
}

_LOCATION_RE = re.compile(
    r"(?:section|gallery)\s+([a-z])\s*,\s*(?:shelf|bay)\s+(\d+)", re.I
)
_SHELF_ZONES = {z["id"]: z for z in FLOOR_PLAN["zones"] if z["kind"] == "shelf"}


def _beacon_anchor(location: str, seed: str = "") -> "tuple[float, float]":
    """Map a location string like 'Gallery A, Bay 1' to a point on the floor plan.

    Anchors sit just off the aisle-facing edge of their gallery, spread down
    that face by bay number so two exhibits in one gallery never overlap.
    The regex also accepts the older 'Section A, Shelf 1' wording.
    Unrecognised locations get a stable pseudo-random spot on the open floor
    rather than being dropped, so a mis-typed location is still visible.
    """
    m = _LOCATION_RE.search(location or "")
    if m:
        zone = _SHELF_ZONES.get(m.group(1).upper())
        if zone:
            bay = max(1, min(SHELVES_PER_SECTION, int(m.group(2))))
            if zone["face"] == "right":
                x = zone["x"] + zone["w"] + ANCHOR_OFFSET
            else:
                x = zone["x"] - ANCHOR_OFFSET
            y = zone["y"] + zone["h"] * bay / (SHELVES_PER_SECTION + 1)
            return round(x, 1), round(y, 1)

    h = int(hashlib.md5((seed or location or "?").encode()).hexdigest()[:8], 16)
    return float(440 + h % 120), float(280 + (h >> 8) % 100)


# ── Shared state (guarded by _lock) ─────────────────────────────────────────

_lock = threading.Lock()
_events: collections.deque = collections.deque(maxlen=MAX_EVENTS)
_images: "collections.OrderedDict[str, str]" = collections.OrderedDict()  # id -> jpeg b64
_audio: "collections.OrderedDict[str, dict]" = collections.OrderedDict()  # id -> {b64, mime}
_beacon_titles: dict = {}        # beacon_uuid -> title
_beacons: dict = {}              # beacon_uuid -> {exhibit_id, title, author, location, x, y}
_devices: dict = {}              # device_id -> live rollup (see _touch_device)
_subscribers: list = []          # list[queue.Queue] for SSE fan-out
_id_counter = itertools.count(1)
_mqtt_connected = threading.Event()


def _next_id() -> str:
    return str(next(_id_counter))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Beacon registry ─────────────────────────────────────────────────────────

def _load_seed_beacons() -> None:
    """Seed beacon names and floor-plan anchors from seed_exhibits.json (best-effort).

    The `location` lives in seed_exhibits.json and in Postgres, but
    beacon_service never publishes it over MQTT, so reading the seed file is
    the only way the dashboard can learn where a beacon physically sits —
    and it keeps this process dependency-free and read-only.
    """
    try:
        exhibits = json.loads(SEED_EXHIBITS.read_text())
        with _lock:
            for b in exhibits:
                uuid = b.get("beacon_uuid")
                title = b.get("title")
                if not uuid:
                    continue
                if title:
                    _beacon_titles.setdefault(uuid, title)
                location = b.get("location") or ""
                x, y = _beacon_anchor(location, seed=uuid)
                _beacons.setdefault(uuid, {
                    "beacon_uuid": uuid,
                    "exhibit_id": b.get("id"),
                    "title": title,
                    "author": b.get("author"),
                    "year": b.get("year"),
                    "genre": b.get("genre"),
                    "location": location,
                    "x": x,
                    "y": y,
                })
    except Exception as e:  # noqa: BLE001 - dashboard must start regardless
        app.logger.warning(f"Could not load seed exhibits: {e}")


# ── Per-device rollup ───────────────────────────────────────────────────────

def _touch_device(device_id: str, ts: str, ev_type: str = "", **fields) -> None:
    """Fold one event into the per-device summary used by the map and cards."""
    device_id = device_id or "unknown"
    with _lock:
        dev = _devices.get(device_id)
        if dev is None:
            dev = {
                "device_id": device_id,
                "first_seen": ts,
                "last_seen": ts,
                "counts": {"image": 0, "generated": 0,
                           "transcript": 0, "ble": 0, "audio": 0},
                "last_beacon_uuid": None,
                "last_rssi": None,
                "last_exhibit_id": None,
                "last_exhibit_title": None,
                "last_location": None,
                "last_fix": None,
                "x": None,
                "y": None,
                "visited": [],
            }
            _devices[device_id] = dev
        dev["last_seen"] = ts
        if ev_type in dev["counts"]:
            dev["counts"][ev_type] += 1
        dev.update({k: v for k, v in fields.items() if v is not None})
        exhibit_id = fields.get("last_exhibit_id")
        if exhibit_id and exhibit_id not in dev["visited"]:
            dev["visited"].append(exhibit_id)


# ── Event publishing to SSE clients ─────────────────────────────────────────

def _emit(event: dict) -> None:
    """Append an event to the feed and fan it out to all SSE subscribers."""
    with _lock:
        _events.append(event)
        subscribers = list(_subscribers)
    for q in subscribers:
        try:
            q.put_nowait(event)
        except queue.Full:
            pass


def _store_image(image_b64: str) -> str:
    img_id = _next_id()
    with _lock:
        _images[img_id] = image_b64
        while len(_images) > MAX_IMAGES:
            _images.popitem(last=False)
    return img_id


def _store_audio(audio_b64: str, mime: str) -> str:
    aud_id = _next_id()
    with _lock:
        _audio[aud_id] = {"b64": audio_b64, "mime": mime}
        while len(_audio) > MAX_AUDIO:
            _audio.popitem(last=False)
    return aud_id


# ── MQTT classification ─────────────────────────────────────────────────────

def _classify(topic: str, payload: dict) -> None:
    """Turn one MQTT message into zero or more dashboard events."""
    device_id = payload.get("device_id", "?")
    ts = payload.get("timestamp") or _now_iso()

    # Retained beacon content: museum/{museum}/beacon/{uuid}/content
    if "/beacon/" in topic and topic.endswith("/content"):
        parts = topic.split("/")
        if len(parts) >= 4:
            uuid = parts[3]
            title = payload.get("title")
            if uuid and title:
                with _lock:
                    # The seed file wins for beacons it already describes. A
                    # retained message can be arbitrarily old — after a content
                    # swap the broker still holds the previous subject's titles,
                    # and letting them through would relabel the map with content
                    # that is no longer in the database. For an unknown beacon
                    # the broker is the only source we have, so take what it says.
                    if uuid not in _beacons:
                        _beacon_titles[uuid] = title
                        x, y = _beacon_anchor(payload.get("location", ""), seed=uuid)
                        _beacons[uuid] = {
                            "beacon_uuid": uuid,
                            "exhibit_id": payload.get("exhibit_id"),
                            "title": title,
                            "author": payload.get("author"),
                            "location": payload.get("location"),
                            "x": x,
                            "y": y,
                        }
        return  # no feed entry for retained content

    suffix = topic.rsplit("/", 1)[-1]

    if suffix == "cam":
        image_b64 = payload.get("image_b64")
        if not image_b64:
            return
        img_id = _store_image(image_b64)
        _touch_device(device_id, ts, "image")
        _emit({
            "id": _next_id(), "ts": ts, "type": "image",
            "device_id": device_id, "image_id": img_id,
            "exhibit_id": payload.get("exhibit_id"),
        })

    elif suffix == "response":
        text = payload.get("text", "")
        rtype = payload.get("response_type", "tts")
        if text:
            _touch_device(device_id, ts, "generated")
            _emit({
                "id": _next_id(), "ts": ts, "type": "generated",
                "device_id": device_id, "text": text,
                "response_type": rtype, "exhibit_id": payload.get("exhibit_id"),
            })
        # QA service echoes the server-side STT transcript back as `query`.
        query = payload.get("query")
        if query:
            _touch_device(device_id, ts, "transcript")
            _emit({
                "id": _next_id(), "ts": ts, "type": "transcript",
                "device_id": device_id, "text": query, "source": "audio",
            })

    elif suffix == "voice":
        transcript = payload.get("transcript", "")
        if transcript:
            _touch_device(device_id, ts, "transcript")
            _emit({
                "id": _next_id(), "ts": ts, "type": "transcript",
                "device_id": device_id, "text": transcript, "source": "text",
            })

    elif suffix == "audio":
        audio_b64 = payload.get("audio_b64")
        if audio_b64:
            aud_id = _store_audio(audio_b64, payload.get("mime_type", "audio/wav"))
            _touch_device(device_id, ts, "audio")
            _emit({
                "id": _next_id(), "ts": ts, "type": "audio",
                "device_id": device_id, "audio_id": aud_id,
            })

    elif suffix == "cmd":
        if payload.get("event") == "beacon_enter":
            uuid = payload.get("beacon_uuid", "")
            rssi = payload.get("rssi")
            with _lock:
                title = _beacon_titles.get(uuid)
                info = dict(_beacons.get(uuid) or {})
            title = title or info.get("title")
            # Carry the anchor on the event itself so the map can move the
            # device straight off the SSE frame with no extra lookup.
            _touch_device(
                device_id, ts, "ble",
                last_beacon_uuid=uuid, last_rssi=rssi, last_fix=ts,
                last_exhibit_id=info.get("exhibit_id"),
                last_exhibit_title=title,
                last_location=info.get("location"),
                x=info.get("x"), y=info.get("y"),
            )
            _emit({
                "id": _next_id(), "ts": ts, "type": "ble",
                "device_id": device_id, "beacon_uuid": uuid,
                "rssi": rssi, "title": title,
                "exhibit_id": info.get("exhibit_id"),
                "location": info.get("location"),
                "x": info.get("x"), "y": info.get("y"),
            })


# ── MQTT thread ─────────────────────────────────────────────────────────────

def _on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        _mqtt_connected.set()
        client.subscribe("museum/#", qos=1)
        app.logger.info("Dashboard connected to MQTT broker; subscribed museum/#")
    else:
        app.logger.error(f"MQTT connect failed, rc={rc}")


def _on_disconnect(client, userdata, rc, properties=None):
    _mqtt_connected.clear()
    if rc != 0:
        app.logger.warning(f"MQTT disconnected (rc={rc}); auto-reconnecting")


def _on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return  # ignore non-JSON traffic
    try:
        _classify(msg.topic, payload)
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f"Failed to classify {msg.topic}: {e}")


def _mqtt_loop():
    client = mqtt.Client(
        client_id=f"dashboard-{os.getpid()}",
        protocol=mqtt.MQTTv5,
    )
    client.username_pw_set(USERNAME, PASSWORD)
    if USE_TLS and CA_CERT:
        import ssl
        client.tls_set(ca_certs=CA_CERT, tls_version=ssl.PROTOCOL_TLS_CLIENT)
    client.on_connect = _on_connect
    client.on_disconnect = _on_disconnect
    client.on_message = _on_message
    client.reconnect_delay_set(min_delay=1, max_delay=30)

    while True:
        try:
            client.connect(BROKER_HOST, BROKER_PORT, keepalive=60)
            client.loop_forever()
        except Exception as e:  # noqa: BLE001 - keep retrying broker connection
            app.logger.warning(f"MQTT connection error: {e}; retrying in 5s")
            time.sleep(5)


# ── Routes ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/snapshot")
def snapshot():
    """Initial paint: everything a freshly-opened tab needs in one request."""
    with _lock:
        events = list(_events)
        devices = [dict(d) for d in _devices.values()]
        connected = _mqtt_connected.is_set()
    return jsonify({
        "connected": connected,
        "broker": f"{BROKER_HOST}:{BROKER_PORT}",
        "server_time": _now_iso(),
        "events": events,
        "devices": devices,
    })


@app.route("/api/floorplan")
def floorplan():
    """Static venue geometry + beacon anchors. Works with the broker down."""
    with _lock:
        beacons = [dict(b) for b in _beacons.values()]
    beacons.sort(key=lambda b: (b.get("location") or "", b.get("title") or ""))
    return jsonify({**FLOOR_PLAN, "beacons": beacons})


@app.route("/api/devices")
def devices():
    with _lock:
        out = [dict(d) for d in _devices.values()]
    out.sort(key=lambda d: d.get("last_seen") or "", reverse=True)
    return jsonify({"devices": out})


@app.route("/events")
def events_stream():
    q: queue.Queue = queue.Queue(maxsize=1000)
    with _lock:
        backlog = list(_events)
        _subscribers.append(q)

    def stream():
        try:
            # Replay current feed so a freshly-opened tab isn't empty.
            for ev in backlog:
                yield f"data: {json.dumps(ev)}\n\n"
            while True:
                try:
                    ev = q.get(timeout=15)
                    yield f"data: {json.dumps(ev)}\n\n"
                except queue.Empty:
                    yield ": keep-alive\n\n"  # comment frame keeps connection open
        finally:
            with _lock:
                if q in _subscribers:
                    _subscribers.remove(q)

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


@app.route("/image/<img_id>")
def image(img_id):
    with _lock:
        b64 = _images.get(img_id)
    if not b64:
        return "Not found", 404
    return Response(base64.b64decode(b64), mimetype="image/jpeg")


@app.route("/audio/<aud_id>")
def audio(aud_id):
    with _lock:
        clip = _audio.get(aud_id)
    if not clip:
        return "Not found", 404
    return Response(base64.b64decode(clip["b64"]), mimetype=clip["mime"])


# ── Entry point ─────────────────────────────────────────────────────────────

def _start_background():
    _load_seed_beacons()
    t = threading.Thread(target=_mqtt_loop, name="mqtt", daemon=True)
    t.start()


if __name__ == "__main__":
    _start_background()
    app.run(host="0.0.0.0", port=PORT, threaded=True)

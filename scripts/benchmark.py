#!/usr/bin/env python3
"""
scripts/benchmark.py

Closed-loop load / latency benchmark for the museum-tour pipeline.

Spins up K concurrent simulated devices. Each device first fires a beacon to
establish exhibit context (so RAG runs exhibit-scoped, like a real visit), then
issues a configurable mix of requests — beacon / voice / photo — one at a time,
waiting for each response before sending the next (closed-loop). Every request
carries a unique `request_id`; the matching response (echoed back by the
services via shared/mqtt_subscriber.py) yields:

  * end-to-end latency (publish -> response received), measured client-side
  * server-side per-stage timings (_timings) and metadata (_meta)

Results are written per-request to a CSV and summarised as p50/p90/p95/p99
per interaction type — ready to drop into a conference paper.

Examples:
    # 30 reps of each interaction on a single device
    python scripts/benchmark.py --reps 30

    # scalability sweep: 8 concurrent devices, voice only, 20 reps each
    python scripts/benchmark.py --concurrency 8 --reps 20 --mix voice

    # narration latency only, custom output file
    python scripts/benchmark.py --mix beacon --reps 50 --out results/beacon.csv

Prereqs: broker + the three services running (./start_services.sh), exhibits
ingested. Uses the same MQTT_* env vars as the rest of the system.
"""

import argparse
import base64
import csv
import io
import json
import os
import statistics
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import paho.mqtt.client as mqtt
from dotenv import load_dotenv

load_dotenv()

BROKER_HOST = os.getenv("MQTT_BROKER_HOST", "localhost")
BROKER_PORT = int(os.getenv("MQTT_BROKER_PORT", "1883"))
USERNAME = os.getenv("MQTT_SERVICE_USERNAME", "service_account")
PASSWORD = os.getenv("MQTT_SERVICE_PASSWORD", "")

def load_exhibits() -> list[dict]:
    """Beacon-backed exhibits, read from the seed file rather than duplicated here.

    A hardcoded copy drifted from the seed data every time the content changed.
    """
    path = Path(__file__).parent.parent / "data" / "exhibits" / "seed_exhibits.json"
    entries = json.loads(path.read_text())
    return [
        {"id": e["id"], "uuid": e["beacon_uuid"], "title": e["title"]}
        for e in entries
        if e.get("beacon_uuid")
    ]


EXHIBITS = load_exhibits()

# Rotated through for voice queries. Deliberately generic, so each one retrieves
# against whichever exhibit the worker is standing at.
QUESTIONS = [
    "What is this exhibit about?",
    "When did this happen?",
    "Why does this matter?",
    "What was Dr. Ambedkar's role in this?",
    "Tell me something surprising about it.",
    "What led up to this?",
]

# Per-request stage-timing columns we know the services emit. Listed explicitly
# so the CSV column order is stable across runs even when a stage is absent.
TIMING_KEYS = [
    "service_ms",        # all services: in-service handler time
    "cache_lookup_ms", "db_ms",                       # beacon
    "stt_ms", "embed_ms", "search_ms",                # qa
    "search_fallback_ms", "llm_ms",                   # qa
    "resize_ms", "vision_llm_ms",                     # vision
]
META_KEYS = [
    "source", "retrieval_scope", "n_results", "top_score", "rate_limited",
    "answer_chars", "query_chars", "audio_bytes", "img_bytes_in",
    "img_bytes_out", "input", "stage",
]


def make_placeholder_image() -> str:
    """A small valid JPEG, matching simulate_device.py's placeholder."""
    try:
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (640, 480), color=(210, 180, 140))
        draw = ImageDraw.Draw(img)
        draw.rectangle([20, 20, 620, 460], outline=(100, 70, 40), width=4)
        draw.text((100, 200), "BENCHMARK PHOTO", fill=(80, 50, 20))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        return base64.b64encode(buf.getvalue()).decode("utf-8")
    except ImportError:
        return (
            "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8U"
            "HRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAARCAABAAEDASIA"
            "AhEBAxEB/8QAFAABAAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/"
            "xAAUAQEAAAAAAAAAAAAAAAAAAAAA/8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAwDAQAC"
            "EQMRAD8AJQAB/9k="
        )


class BenchDevice:
    """One simulated device: own MQTT client, own response topic, closed-loop."""

    def __init__(self, device_id: str, museum_id: str, timeout: float):
        self.device_id = device_id
        self.museum_id = museum_id
        self.timeout = timeout
        self.session_id = f"bench-{uuid.uuid4().hex[:8]}"

        self.client = mqtt.Client(
            client_id=f"bench-{device_id}-{os.getpid()}", protocol=mqtt.MQTTv5
        )
        self.client.username_pw_set(USERNAME, PASSWORD)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

        # request_id -> {"event": Event, "payload": dict|None}
        self._pending: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._connected = threading.Event()

    # ── MQTT plumbing ─────────────────────────────────────────────────────────

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            client.subscribe(
                f"museum/{self.museum_id}/device/{self.device_id}/response", qos=1
            )
            self._connected.set()

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
        except Exception:
            return
        rid = payload.get("request_id")
        if rid is None:
            return
        with self._lock:
            slot = self._pending.get(rid)
        if slot is not None:
            slot["payload"] = payload
            slot["event"].set()

    def connect(self):
        self.client.connect(BROKER_HOST, BROKER_PORT, keepalive=60)
        self.client.loop_start()
        if not self._connected.wait(timeout=10):
            raise RuntimeError(f"{self.device_id}: MQTT connect timed out")

    def close(self):
        self.client.loop_stop()
        try:
            self.client.disconnect()
        except Exception:
            pass

    # ── One request/response round-trip ───────────────────────────────────────

    def request(self, subtopic: str, body: dict) -> dict:
        """Publish a request and block until the matching response (or timeout).

        Returns a flat record dict with e2e_ms and server timings/meta.
        """
        rid = uuid.uuid4().hex
        ev = threading.Event()
        with self._lock:
            self._pending[rid] = {"event": ev, "payload": None}

        payload = dict(body)
        payload.update({
            "device_id": self.device_id,
            "museum_id": self.museum_id,
            "session_id": self.session_id,
            "request_id": rid,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        topic = f"museum/{self.museum_id}/device/{self.device_id}/{subtopic}"

        t0 = time.perf_counter()
        self.client.publish(topic, json.dumps(payload), qos=1)
        got = ev.wait(timeout=self.timeout)
        e2e_ms = round((time.perf_counter() - t0) * 1000.0, 2)

        with self._lock:
            slot = self._pending.pop(rid, None)
        resp = slot["payload"] if (slot and got) else None

        rec = {
            "device_id": self.device_id,
            "request_id": rid,
            "e2e_ms": e2e_ms if got else None,
            "timed_out": not got,
            "response_type": (resp or {}).get("response_type"),
        }
        timings = (resp or {}).get("_timings", {}) or {}
        meta = (resp or {}).get("_meta", {}) or {}
        for k in TIMING_KEYS:
            rec[k] = timings.get(k)
        for k in META_KEYS:
            rec[k] = meta.get(k)
        return rec

    # ── Interaction helpers ───────────────────────────────────────────────────

    def beacon(self, exhibit: dict) -> dict:
        rec = self.request("cmd", {
            "event": "beacon_enter", "beacon_uuid": exhibit["uuid"], "rssi": -62,
        })
        rec["interaction"] = "beacon"
        return rec

    def voice(self, exhibit: dict, question: str) -> dict:
        rec = self.request("voice", {
            "event": "voice_query", "transcript": question, "exhibit_id": exhibit["id"],
        })
        rec["interaction"] = "voice"
        return rec

    def photo(self, exhibit: dict, image_b64: str) -> dict:
        rec = self.request("cam", {
            "event": "camera_capture", "image_b64": image_b64, "exhibit_id": exhibit["id"],
        })
        rec["interaction"] = "photo"
        return rec


def worker(idx: int, args, image_b64: str, out_records: list, errors: list):
    """Run one device's full closed-loop workload; append records to out_records."""
    device_id = f"{args.device_prefix}-{idx:03d}"
    exhibit = EXHIBITS[idx % len(EXHIBITS)]
    dev = BenchDevice(device_id, args.museum_id, args.timeout)
    local: list[dict] = []
    try:
        dev.connect()
        # Establish exhibit context so voice/photo run against a current exhibit.
        ctx = dev.beacon(exhibit)
        if "beacon" in args.mix and not args.no_warmup_in_results:
            pass  # the context beacon itself isn't counted; real ones come below

        total_iters = args.warmup + args.reps
        for it in range(total_iters):
            is_warmup = it < args.warmup
            for kind in args.mix:
                if kind == "beacon":
                    rec = dev.beacon(EXHIBITS[(idx + it) % len(EXHIBITS)])
                elif kind == "voice":
                    rec = dev.voice(exhibit, QUESTIONS[(idx + it) % len(QUESTIONS)])
                elif kind == "photo":
                    rec = dev.photo(exhibit, image_b64)
                else:
                    continue
                rec["worker"] = idx
                rec["iter"] = it
                rec["warmup"] = is_warmup
                rec["ts"] = datetime.now(timezone.utc).isoformat()
                local.append(rec)
                if not is_warmup:
                    print(
                        f"  [{device_id}] {rec['interaction']:6} "
                        f"e2e={rec['e2e_ms']}ms"
                        + (" TIMEOUT" if rec["timed_out"] else "")
                    )
    except Exception as e:
        errors.append(f"{device_id}: {type(e).__name__}: {e}")
    finally:
        dev.close()
    out_records.extend(local)


# ── Stats / reporting ─────────────────────────────────────────────────────────

def _pct(values: list[float], q: float) -> float:
    """Linear-interpolated percentile (q in 0..100). values need not be sorted."""
    if not values:
        return float("nan")
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    pos = (len(s) - 1) * (q / 100.0)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    frac = pos - lo
    return s[lo] + (s[hi] - s[lo]) * frac


def summarise(records: list[dict]) -> None:
    """Print per-interaction latency summary over non-warmup, non-timeout rows."""
    measured = [r for r in records if not r.get("warmup")]
    interactions = sorted({r["interaction"] for r in measured})

    print("\n" + "=" * 78)
    print("  LATENCY SUMMARY (end-to-end, ms) — non-warmup requests")
    print("=" * 78)
    header = f"  {'interaction':10} {'n':>4} {'ok':>4} {'p50':>8} {'p90':>8} {'p95':>8} {'p99':>8} {'mean':>8} {'max':>8}"
    print(header)
    print("  " + "-" * 74)
    for it in interactions:
        rows = [r for r in measured if r["interaction"] == it]
        ok = [r["e2e_ms"] for r in rows if not r["timed_out"] and r["e2e_ms"] is not None]
        n, n_ok = len(rows), len(ok)
        if not ok:
            print(f"  {it:10} {n:>4} {n_ok:>4} {'—':>8} (all timed out)")
            continue
        print(
            f"  {it:10} {n:>4} {n_ok:>4} "
            f"{_pct(ok, 50):>8.1f} {_pct(ok, 90):>8.1f} {_pct(ok, 95):>8.1f} "
            f"{_pct(ok, 99):>8.1f} {statistics.mean(ok):>8.1f} {max(ok):>8.1f}"
        )

    # Per-stage mean breakdown (where the latency budget goes).
    print("\n  PER-STAGE MEAN (ms) — server-side, where reported")
    print("  " + "-" * 74)
    for it in interactions:
        rows = [r for r in measured if r["interaction"] == it and not r["timed_out"]]
        if not rows:
            continue
        stage_means = []
        for k in TIMING_KEYS:
            vals = [r[k] for r in rows if r.get(k) is not None]
            if vals:
                stage_means.append(f"{k}={statistics.mean(vals):.1f}")
        print(f"  {it:10} " + "  ".join(stage_means))

    # Retrieval-scope distribution (voice only) — supports the scoping ablation.
    voice_rows = [r for r in measured if r["interaction"] == "voice" and r.get("retrieval_scope")]
    if voice_rows:
        from collections import Counter
        dist = Counter(r["retrieval_scope"] for r in voice_rows)
        print("\n  RETRIEVAL SCOPE (voice): " + ", ".join(f"{k}={v}" for k, v in dist.items()))

    timeouts = sum(1 for r in measured if r["timed_out"])
    rl = sum(1 for r in measured if r.get("rate_limited"))
    print(f"\n  Timeouts: {timeouts}/{len(measured)}   Rate-limited (429): {rl}")
    print("=" * 78)


def write_csv(records: list[dict], path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    base_cols = [
        "ts", "worker", "device_id", "iter", "warmup", "interaction",
        "request_id", "response_type", "e2e_ms", "timed_out",
    ]
    fieldnames = base_cols + TIMING_KEYS + META_KEYS
    # Include any unexpected keys deterministically at the end.
    extra = sorted({k for r in records for k in r} - set(fieldnames))
    fieldnames += extra
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in records:
            w.writerow(r)
    print(f"\nWrote {len(records)} rows -> {path}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--concurrency", type=int, default=1, help="number of concurrent simulated devices")
    p.add_argument("--reps", type=int, default=20, help="measured iterations per device per interaction")
    p.add_argument("--warmup", type=int, default=2, help="warmup iterations discarded from stats")
    p.add_argument("--mix", default="beacon,voice,photo",
                   help="comma list of interactions: beacon,voice,photo")
    p.add_argument("--timeout", type=float, default=30.0, help="per-request response timeout (s)")
    p.add_argument("--museum-id", default="demo-library")
    p.add_argument("--device-prefix", default="pi-bench")
    p.add_argument("--out", default="results/benchmark.csv", help="CSV output path")
    p.add_argument("--no-warmup-in-results", action="store_true",
                   help="(reserved) keep context-beacon out of results")
    args = p.parse_args()

    args.mix = [m.strip() for m in args.mix.split(",") if m.strip()]
    bad = [m for m in args.mix if m not in ("beacon", "voice", "photo")]
    if bad:
        p.error(f"unknown interaction(s) in --mix: {bad}")

    image_b64 = make_placeholder_image() if "photo" in args.mix else ""

    print(f"\n{'='*78}")
    print(f"  Museum-Tour Benchmark")
    print(f"  broker={BROKER_HOST}:{BROKER_PORT}  museum={args.museum_id}")
    print(f"  concurrency={args.concurrency}  reps={args.reps}  warmup={args.warmup}")
    print(f"  mix={args.mix}  timeout={args.timeout}s")
    print(f"{'='*78}\n")

    records: list[dict] = []
    errors: list[str] = []
    threads = []
    wall_t0 = time.perf_counter()
    for i in range(args.concurrency):
        t = threading.Thread(target=worker, args=(i, args, image_b64, records, errors), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    wall_s = time.perf_counter() - wall_t0

    if errors:
        print("\nWorker errors:")
        for e in errors:
            print(f"  ! {e}")

    if not records:
        print("\nNo records collected — are the broker and services running?")
        sys.exit(1)

    write_csv(records, args.out)
    summarise(records)

    measured = [r for r in records if not r.get("warmup") and not r["timed_out"]]
    if wall_s > 0 and measured:
        print(f"\n  Wall time: {wall_s:.1f}s   "
              f"Throughput: {len(measured)/wall_s:.2f} completed req/s "
              f"(concurrency={args.concurrency})")


if __name__ == "__main__":
    main()

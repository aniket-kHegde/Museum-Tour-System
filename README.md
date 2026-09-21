# Museum Tour System

An AI-powered, self-guided tour device for museums and memorials. A Raspberry Pi
worn or carried by the visitor detects which exhibit they are standing near,
narrates it aloud, and answers spoken questions and photo queries — all grounded
in the museum's own content.

The bundled demo is loaded with the life and work of **Dr. B.R. Ambedkar**
(see [AMBEDKAR_DEMO.md](AMBEDKAR_DEMO.md)). The content lives in a single JSON
file, so the same system can be pointed at any other subject with no code changes.

## Features

- **Proximity narration** — BLE beacons at each exhibit trigger a spoken introduction.
- **Voice Q&A** — visitors ask questions out loud; answers come from a RAG pipeline
  (embeddings + Qdrant) and are grounded only in the museum's exhibit content.
- **Photo queries** — the device camera captures an image and a vision LLM describes
  it in the context of the current exhibit.
- **Live dashboard** — a read-only web view of camera frames, transcripts, answers,
  active beacons and device position on a floor plan.
- **Benchmarking** — a closed-loop load generator and analysis script for latency and
  scalability measurements.
- **Subject-agnostic** — swap `data/exhibits/seed_exhibits.json` and re-ingest.

## Architecture

```
  BLE beacons at each exhibit
            │  RSSI proximity
            ▼
  Raspberry Pi device ── mic · camera · speaker
            │  MQTT (paho)
            ▼
  Mosquitto broker ──────────────► Dashboard (Flask + SSE, read-only)
            │
   ┌────────┼─────────────┐
   ▼        ▼             ▼
 Beacon    Q&A          Vision
 service   service      service
   │        │  │          │
Postgres  Qdrant └─ Gemini / Claude (STT, answers, image description)
              │
            Redis  ← per-device session state and rate limiting
```

| Component | Role |
|---|---|
| `device/` | Pi agent: BLE scan, mic capture, camera, offline TTS, local cache |
| `device/beacon_bridge.py` | Laptop-side BLE scanner for Pis with no Bluetooth controller; publishes beacon events on the Pi's behalf |
| `services/beacon_service` | Beacon UUID → exhibit lookup → narration script |
| `services/qa_service` | Audio/text query → transcription → RAG → LLM answer |
| `services/vision_service` | Photo + current exhibit context → vision LLM description |
| `dashboard/` | Flask app that observes `museum/#` and streams it to the browser |

Devices publish to `museum/<museum_id>/device/<device_id>/…` topics; services reply on
the device's `…/response` topic.

## Quick start

### Prerequisites

- Docker and Docker Compose
- Python 3.11+
- A Gemini API key (default) or an Anthropic API key

### 1. Configure

```bash
cp .env.example .env
```

Edit `.env` and set your key. For the Gemini setup used in the demo:

```ini
LLM_PROVIDER=gemini
LLM_MODEL=gemini-2.5-flash
VISION_MODEL=gemini-2.5-flash
STT_MODEL=gemini-2.5-flash
GEMINI_API_KEY=your-key-here
```

> **Note:** Voice input is transcribed server-side by Gemini, so spoken queries
> require `LLM_PROVIDER=gemini`. With Anthropic, text and vision work but audio does not.

`.env` is git-ignored; never commit it.

### 2. Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r scripts/requirements.txt
```

### 3. Start infrastructure

```bash
docker compose up -d mosquitto postgres qdrant redis
```

### 4. Load the exhibits

```bash
python scripts/ingest_exhibits.py
```

This seeds Postgres and Qdrant and downloads the `all-MiniLM-L6-v2` embedding model
(~90 MB, first run only). It also prunes exhibits that are no longer in the seed file.

### 5. Start the services

```bash
./start_services.sh
```

Starts the Docker infra, the beacon, Q&A and vision services (logs in `services/*.out`),
the BLE bridge, and then runs a health check. Flags: `--no-bridge`, `--no-check`.
`./stop_services.sh` shuts them down.

Note that `start_services.sh` starts only mosquitto, postgres and qdrant in Docker and
expects Redis to already be running on the host. If you brought Redis up with Docker
in step 3, that satisfies it.

Or run each service by hand in its own terminal:

```bash
python services/beacon_service/main.py
python services/qa_service/main.py
python services/vision_service/main.py
```

### 6. Try it without hardware

```bash
python scripts/simulate_device.py       # interactive: trigger beacons, ask, take a photo
python scripts/health_check.py --e2e    # automated infra + end-to-end check
```

### 7. Open the dashboard

```bash
pip install -r dashboard/requirements.txt
cd dashboard && python app.py           # http://localhost:5005
```

## Running on a Raspberry Pi

```bash
# On the Pi
sudo apt update
sudo apt install python3-picamera2 python3-pip espeak ffmpeg bluetooth bluez
python3 -m venv --system-site-packages .venv && source .venv/bin/activate
pip install -r device/requirements.txt

# Generate device_config.yaml (and optionally a systemd unit)
python scripts/provision_device.py \
  --device-id pi-001 \
  --museum-id demo-library \
  --broker-host <broker-ip> \
  --install-systemd

python device/agent.py
```

- Speech-to-text runs on the server, so the Pi only captures and ships audio: no
  Whisper or PyTorch is needed on the device.
- `--system-site-packages` lets the venv see the apt-installed `picamera2`.
- If the Pi has no working Bluetooth controller, run the bridge on a laptop near it:

  ```bash
  python device/beacon_bridge.py --broker <broker-ip> --device-id pi-001
  ```

  and start the agent with `BEACON_SOURCE=external python device/agent.py`.

## Configuration

See [.env.example](.env.example) for the full list.

| Variable | Description |
|---|---|
| `LLM_PROVIDER` | `gemini` or `anthropic` |
| `LLM_MODEL` / `VISION_MODEL` / `STT_MODEL` | Model names for answers, images and transcription |
| `GEMINI_API_KEY` / `ANTHROPIC_API_KEY` | Provider credentials |
| `MQTT_BROKER_HOST`, `MQTT_BROKER_PORT` | Broker address (default `localhost:1883`) |
| `MQTT_SERVICE_USERNAME` / `MQTT_SERVICE_PASSWORD` | Credentials the services use with the broker |
| `POSTGRES_*`, `REDIS_*`, `QDRANT_*` | Datastore connection settings |
| `EMBEDDING_MODEL` | Local embedding model (no API key needed) |

Set your own `MQTT_SERVICE_PASSWORD` and `POSTGRES_PASSWORD` in `.env`; there are no
built-in defaults (`docker compose` refuses to start without `POSTGRES_PASSWORD`). The bundled
Mosquitto config allows anonymous connections on a trusted LAN and has no TLS. Enable
authentication and TLS before any real deployment.

## Adding exhibits or changing the subject

1. Edit `data/exhibits/seed_exhibits.json`.
2. Run `python scripts/ingest_exhibits.py`.
3. For a beacon-backed exhibit, program a physical beacon with its `beacon_uuid` and add
   the UUID to `KNOWN_UUIDS` in `device/beacon_bridge.py` (unlisted beacons are dropped).

```json
{
  "id": "exhibit-001",
  "beacon_uuid": "<UUID programmed into the beacon, or null>",
  "title": "The Starry Night",
  "author": "Vincent van Gogh",
  "year": 1889,
  "genre": "Post-Impressionism",
  "location": "Gallery A, Bay 1",
  "tts_script": "Spoken when the beacon fires. 2-4 sentences.",
  "full_content": "Everything the Q&A is allowed to answer from."
}
```

Entries with `beacon_uuid: null` are searchable by voice from anywhere but never trigger a
narration. A full ingest **prunes** exhibits missing from the seed file, which stops the
previous subject's content leaking into answers when you switch.

## Benchmarking

```bash
python scripts/benchmark.py --concurrency 8 --reps 20 --mix voice
python scripts/analyze_results.py --csv results/latency.csv
```

The benchmark runs concurrent simulated devices, records end-to-end and per-stage
latency to CSV in `results/`, and reports p50/p90/p95/p99 per interaction type.
Requires the broker and services to be running.

## Project structure

```
├── device/            Raspberry Pi agent and laptop BLE bridge
├── services/          beacon_service · qa_service · vision_service · shared/
├── dashboard/         Flask + SSE live dashboard
├── database/migrations/   PostgreSQL schema (run in order)
├── data/exhibits/     seed_exhibits.json (content)
├── scripts/           ingest, simulate, provision, health check, benchmark, analyze
├── broker/            mosquitto.conf
├── results/           benchmark CSVs
├── docs/              IoT design methodology
├── docker-compose.yml
├── start_services.sh / stop_services.sh
└── .env.example
```

## Documentation

- [AMBEDKAR_DEMO.md](AMBEDKAR_DEMO.md) — the demo's content set, galleries and beacon map
- [docs/IOT_DESIGN_METHODOLOGY.md](docs/IOT_DESIGN_METHODOLOGY.md) — design methodology and system reference

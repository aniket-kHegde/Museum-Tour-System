#!/usr/bin/env bash
#
# start_services.sh — bring up EVERYTHING for the museum tour demo.
#
#   1. Docker infra:  mosquitto, postgres, qdrant  (redis runs on the host)
#   2. App services:  beacon, qa, vision  (backgrounded with nohup -> services/*.out)
#   3. BLE bridge:    device/beacon_bridge.py  (laptop-side scanner -> device/beacon_bridge.out)
#   4. Health check:  scripts/health_check.py --e2e
#
# Usage:
#   ./start_services.sh              # start everything (infra + services + bridge + check)
#   ./start_services.sh --no-bridge  # skip the BLE bridge (e.g. bridge runs on another laptop)
#   ./start_services.sh --no-check   # skip the health check at the end
#
# Flags can be combined. Re-running is safe: anything already up is left alone.

set -euo pipefail

# Always operate from the project root (the dir this script lives in),
# so it works no matter where you call it from.
cd "$(dirname "$0")"

PY=".venv/bin/python"
SERVICES=(beacon qa vision)

# Parse flags (order-independent).
RUN_BRIDGE=1
RUN_CHECK=1
for arg in "$@"; do
  case "$arg" in
    --no-bridge) RUN_BRIDGE=0 ;;
    --no-check)  RUN_CHECK=0 ;;
    *) echo "Unknown flag: $arg" >&2; exit 2 ;;
  esac
done

# --- 1. Infra -----------------------------------------------------------------
echo "==> Starting Docker infra (mosquitto, postgres, qdrant)..."
docker compose up -d mosquitto postgres qdrant

# --- 2. App services ----------------------------------------------------------
start_service() {
  local name="$1"
  local main="services/${name}_service/main.py"
  local log="services/${name}.out"

  # Already running? pgrep matches the full command line (-f).
  if pgrep -f "$main" >/dev/null 2>&1; then
    echo "==> ${name}_service already running — skipping."
    return
  fi

  echo "==> Starting ${name}_service (logs -> ${log})..."
  nohup "$PY" "$main" >"$log" 2>&1 &
}

for svc in "${SERVICES[@]}"; do
  start_service "$svc"
done

# --- 3. BLE bridge (laptop-side beacon scanner) -------------------------------
if [[ "$RUN_BRIDGE" == "1" ]]; then
  BRIDGE_MAIN="device/beacon_bridge.py"
  BRIDGE_LOG="device/beacon_bridge.out"
  if pgrep -f "$BRIDGE_MAIN" >/dev/null 2>&1; then
    echo "==> beacon_bridge already running — skipping."
  else
    echo "==> Starting beacon_bridge (logs -> ${BRIDGE_LOG})..."
    echo "    (needs Bluetooth ON + terminal BLE permission on macOS)"
    nohup "$PY" "$BRIDGE_MAIN" >"$BRIDGE_LOG" 2>&1 &
  fi
fi

# Give services + bridge a moment to bind their connections before we probe them.
sleep 3

# --- 4. Health check ----------------------------------------------------------
if [[ "$RUN_CHECK" == "1" ]]; then
  echo "==> Running health check..."
  "$PY" scripts/health_check.py --e2e
fi

# Report which MQTT clients actually connected (a live process can still be
# disconnected — this catches that).
echo "==> Connected MQTT clients:"
docker logs museum-mqtt 2>&1 | grep 'New client connected' \
  | grep -iE 'beacon|qa|vision|bridge' | tail -6 || true

echo "==> Done. Tail a log with:  tail -f services/qa.out  (or device/beacon_bridge.out)"

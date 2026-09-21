#!/usr/bin/env bash
#
# stop_services.sh — tear down everything for the museum tour demo.
#
#   1. BLE bridge:    device/beacon_bridge.py  (the nohup'd laptop-side scanner)
#   2. App services:  beacon, qa, vision  (the nohup'd python procs)
#   3. Docker infra:  mosquitto, postgres, qdrant  (redis on the host is left alone)
#
# Usage:
#   ./stop_services.sh             # stop bridge + app services + docker infra
#   ./stop_services.sh --apps-only # stop only bridge + python services, leave docker up
#
# Re-running is safe: anything already stopped is skipped.

set -uo pipefail   # not -e: a "nothing to kill" non-match shouldn't abort the script

cd "$(dirname "$0")"

SERVICES=(beacon qa vision)

# --- 0. BLE bridge ------------------------------------------------------------
if pgrep -f "device/beacon_bridge.py" >/dev/null 2>&1; then
  echo "==> Stopping beacon_bridge..."
  pkill -f "device/beacon_bridge.py" 2>/dev/null || true
else
  echo "==> beacon_bridge not running — skipping."
fi

# --- 1. App services ----------------------------------------------------------
stop_service() {
  local name="$1"
  local main="services/${name}_service/main.py"

  # -f matches the full command line; capture PIDs so we can report them.
  local pids
  pids="$(pgrep -f "$main" || true)"

  if [[ -z "$pids" ]]; then
    echo "==> ${name}_service not running — skipping."
    return
  fi

  echo "==> Stopping ${name}_service (pids: ${pids//$'\n'/ })..."
  # Ask politely first (SIGTERM) so the service can clean up.
  kill $pids 2>/dev/null || true
}

for svc in "${SERVICES[@]}"; do
  stop_service "$svc"
done

# Give them a moment to exit on SIGTERM, then force-kill any stragglers.
sleep 2
for svc in "${SERVICES[@]}"; do
  main="services/${svc}_service/main.py"
  if pgrep -f "$main" >/dev/null 2>&1; then
    echo "==> ${svc}_service still alive — sending SIGKILL."
    pkill -9 -f "$main" || true
  fi
done
if pgrep -f "device/beacon_bridge.py" >/dev/null 2>&1; then
  echo "==> beacon_bridge still alive — sending SIGKILL."
  pkill -9 -f "device/beacon_bridge.py" || true
fi

# --- 2. Docker infra ----------------------------------------------------------
if [[ "${1:-}" != "--apps-only" ]]; then
  echo "==> Stopping Docker infra (mosquitto, postgres, qdrant)..."
  docker compose stop mosquitto postgres qdrant
fi

echo "==> Done."

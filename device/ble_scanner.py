"""
device/ble_scanner.py

Scans for BLE beacons using bleak (async, cross-platform).
Detects the nearest beacon above the RSSI threshold.
Publishes beacon_enter / beacon_exit events via the MQTT client.
Debounces to avoid re-triggering on the same beacon repeatedly.
"""

import asyncio
import time
from typing import Optional

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from loguru import logger


class BLEScanner:
    @staticmethod
    async def adapter_available() -> bool:
        """Quick probe: is a usable BLE controller present?

        Returns False on hosts with no/disabled Bluetooth (e.g. a Pi whose
        onboard chip is dead and has no USB dongle), so the caller can fall
        back to the simulated beacon instead of erroring on every scan.
        """
        try:
            await BleakScanner.discover(timeout=0.5)
            return True
        except Exception as e:
            logger.warning(f"No usable BLE adapter ({e})")
            return False

    def __init__(self, cfg, mqtt_client, session, allowed_uuids=None, sticky=False):
        self.cfg = cfg
        self.mqtt = mqtt_client
        self.session = session

        # If set, only these UUIDs are ever considered; all other beacons are
        # ignored (so we never trigger on items we have no content for).
        self._allowed = {u.lower() for u in allowed_uuids} if allowed_uuids else None
        # Sticky: once a beacon is active, stay on it through brief signal
        # drops. Only switch when a *different* allowed beacon appears. Prevents
        # the enter/leave/re-enter flapping that re-narrates the same exhibit.
        self._sticky = sticky

        self._scan_interval = cfg.scan_interval_ms / 1000.0
        self._rssi_threshold = cfg.rssi_threshold
        self._debounce = cfg.debounce_seconds

        # Track state
        self._beacon_rssi: dict[str, int] = {}   # uuid → latest RSSI
        self._last_triggered: dict[str, float] = {}  # uuid → last trigger time
        self._active_uuid: Optional[str] = None

    async def scan_loop(self):
        """Continuously scan for BLE beacons. Runs forever."""
        logger.info(
            f"BLE scanner started "
            f"(threshold={self._rssi_threshold} dBm, "
            f"debounce={self._debounce}s)"
        )

        while True:
            try:
                await self._scan_once()
            except Exception as e:
                logger.warning(f"BLE scan error: {e}")
            await asyncio.sleep(self._scan_interval)

    async def _scan_once(self):
        """Run one BLE scan and process results."""
        devices: dict[str, tuple[BLEDevice, AdvertisementData]] = (
            await BleakScanner.discover(
                timeout=self._scan_interval * 0.8,
                return_adv=True,
            )
        )

        # Collect all beacons above threshold, ignoring any not in the allow-list
        visible: dict[str, int] = {}
        for addr, (device, adv) in devices.items():
            uuid = self._extract_beacon_uuid(device, adv)
            if not uuid:
                continue
            if self._allowed is not None and uuid not in self._allowed:
                continue  # unknown beacon — we have no content for it
            if adv.rssi and adv.rssi >= self._rssi_threshold:
                visible[uuid] = adv.rssi

        self._beacon_rssi = visible

        if not visible:
            # No (known) beacons in range. In sticky mode keep the current
            # exhibit so a brief drop-out doesn't reset context; otherwise exit.
            if self._active_uuid and not self._sticky:
                logger.info(f"Beacon lost: {self._active_uuid}")
                self.session.leave_exhibit()
                self._active_uuid = None
            return

        # Pick the strongest beacon
        best_uuid = max(visible, key=visible.get)
        best_rssi = visible[best_uuid]

        if best_uuid == self._active_uuid:
            return  # Already at this beacon

        # Debounce check
        now = time.monotonic()
        last = self._last_triggered.get(best_uuid, 0)
        if now - last < self._debounce:
            return

        self._active_uuid = best_uuid
        self._last_triggered[best_uuid] = now

        logger.info(f"Beacon entered: {best_uuid} (RSSI {best_rssi} dBm)")
        self.mqtt.publish_beacon_enter(best_uuid, best_rssi)

    def _extract_beacon_uuid(
        self, device: BLEDevice, adv: AdvertisementData
    ) -> Optional[str]:
        """
        Extract UUID from iBeacon or Eddystone advertisement.

        iBeacon: manufacturer data key 0x004C, bytes 2-17 are the UUID.
        Eddystone: service UUID 0xFEAA present; use device address as key.

        For the demo we use the device address as a proxy for UUID
        if no proper beacon format is detected — useful for testing with
        any BLE peripheral.
        """
        # iBeacon (Apple manufacturer ID = 0x004C)
        if 0x004C in (adv.manufacturer_data or {}):
            data = adv.manufacturer_data[0x004C]
            if len(data) >= 18 and data[0] == 0x02:
                uuid_bytes = data[2:18]
                uuid_str = (
                    f"{uuid_bytes[0:4].hex()}-"
                    f"{uuid_bytes[4:6].hex()}-"
                    f"{uuid_bytes[6:8].hex()}-"
                    f"{uuid_bytes[8:10].hex()}-"
                    f"{uuid_bytes[10:16].hex()}"
                )
                return uuid_str.lower()

        # Eddystone (service UUID 0xFEAA)
        if "0000feaa-0000-1000-8000-00805f9b34fb" in (adv.service_uuids or []):
            # Use device address as a stable key
            return device.address.lower().replace(":", "")

        # Fallback: use any advertised service UUID
        if adv.service_uuids:
            return adv.service_uuids[0].lower()

        return None

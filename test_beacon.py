"""
test_beacon.py

Standalone BLE iBeacon sniffer — run this BEFORE wiring beacons into agent.py
to confirm your phone's beacon is actually being seen and that its UUID matches
what the backend expects.

Set up an iBeacon in nRF Connect (Advertiser → iBeacon) with one of the UUIDs
below, then run:

    .venv/bin/python test_beacon.py

You should see your beacon appear with its UUID / Major / Minor / RSSI, and a
"✓ KNOWN EXHIBIT" tag if the UUID matches a seeded exhibit.

Works on macOS (CoreBluetooth) and Linux/Raspberry Pi (BlueZ). On macOS the
device address is a system-assigned UUID, not a MAC — that's expected.
"""

import asyncio
import time

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

# Same UUIDs seeded from data/exhibits/seed_exhibits.json into the beacons table.
# Set your nRF Connect iBeacon UUID to one of these to get a "KNOWN EXHIBIT" match.
KNOWN_BEACONS = {
    "550e8400-e29b-41d4-a716-446655440001": "Annihilation of Caste",
    "550e8400-e29b-41d4-a716-446655440002": "The Buddha and His Dhamma",
    "550e8400-e29b-41d4-a716-446655440003": "The Constitution of India",
    "550e8400-e29b-41d4-a716-446655440004": "Deekshabhoomi, Nagpur",
    "550e8400-e29b-41d4-a716-446655440005": "Chaitya Bhoomi, Mumbai",
}

APPLE_MANUFACTURER_ID = 0x004C


def parse_ibeacon(adv: AdvertisementData):
    """Return (uuid, major, minor, tx_power) if adv is an iBeacon, else None.

    iBeacon layout inside Apple (0x004C) manufacturer data:
        byte 0      : 0x02  (type = iBeacon)
        byte 1      : 0x15  (length = 21)
        bytes 2-17  : 128-bit proximity UUID
        bytes 18-19 : major (big-endian)
        bytes 20-21 : minor (big-endian)
        byte 22     : measured TX power at 1m (signed)
    """
    data = (adv.manufacturer_data or {}).get(APPLE_MANUFACTURER_ID)
    if not data or len(data) < 23 or data[0] != 0x02 or data[1] != 0x15:
        return None

    u = data[2:18]
    uuid = (
        f"{u[0:4].hex()}-{u[4:6].hex()}-{u[6:8].hex()}-"
        f"{u[8:10].hex()}-{u[10:16].hex()}"
    ).lower()
    major = int.from_bytes(data[18:20], "big")
    minor = int.from_bytes(data[20:22], "big")
    tx_power = int.from_bytes(data[22:23], "big", signed=True)
    return uuid, major, minor, tx_power


async def main(scan_seconds: float = 4.0):
    print("Scanning for iBeacons... (Ctrl-C to stop)")
    print(f"Known exhibit UUIDs loaded: {len(KNOWN_BEACONS)}\n")

    while True:
        devices: dict[str, tuple[BLEDevice, AdvertisementData]] = (
            await BleakScanner.discover(timeout=scan_seconds, return_adv=True)
        )

        beacons = []
        for addr, (device, adv) in devices.items():
            parsed = parse_ibeacon(adv)
            if parsed:
                beacons.append((addr, device, adv, parsed))

        ts = time.strftime("%H:%M:%S")
        if not beacons:
            print(f"[{ts}] no iBeacons in range "
                  f"({len(devices)} BLE devices total)")
        else:
            print(f"[{ts}] {len(beacons)} iBeacon(s):")
            # strongest first
            beacons.sort(key=lambda b: b[2].rssi or -999, reverse=True)
            for addr, device, adv, (uuid, major, minor, tx) in beacons:
                exhibit = KNOWN_BEACONS.get(uuid)
                tag = f"  ✓ KNOWN EXHIBIT: {exhibit}" if exhibit else "  (unknown UUID)"
                print(
                    f"    {uuid}  major={major} minor={minor} "
                    f"rssi={adv.rssi} dBm  tx@1m={tx}{tag}"
                )
        print()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")

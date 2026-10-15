"""One-off: ask the ESP32 to pair its BLE key with the car (no Home Assistant needed).

Run it sitting in the car, then tap the NFC key card on the console and confirm on the screen:
    uv run python -m volture.pair [--esphome-host volture.local]
"""
from __future__ import annotations

import argparse
import asyncio

from aioesphomeapi import APIClient

from .car import ESPHOME_API_PORT
from .secrets import read_secret

PAIR_ENTITY = "pair_ble_key"  # button exposed by the yoziru/esphome-tesla-ble firmware


async def pair(host: str, noise_psk: str) -> None:
    client = APIClient(host, ESPHOME_API_PORT, None, noise_psk=noise_psk)
    await client.connect(login=True)
    try:
        entities, _services = await client.list_entities_services()
        keys = {entity.object_id: entity.key for entity in entities}
        if PAIR_ENTITY not in keys:
            raise SystemExit(f"ESP32 firmware has no '{PAIR_ENTITY}' button")
        client.button_command(keys[PAIR_ENTITY])
        # button_command only queues the message; give it time to leave before disconnecting.
        await asyncio.sleep(1)
        print("Pairing requested: tap the NFC key card on the console now and confirm on the car screen.")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--esphome-host", default="volture.local")
    a = ap.parse_args()
    asyncio.run(pair(a.esphome_host, read_secret("api_encryption_key")))

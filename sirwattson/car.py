"""Car adapters: turn a controller Command into an action on the (simulated or real) car.

Both raise on failure; main.py catches, logs and keeps the control loop running.
"""
from __future__ import annotations

import aiohttp
from aioesphomeapi import APIClient

from .controller import Command

# object_id of the entities exposed by the yoziru/esphome-tesla-ble firmware (listed from the flashed ESP32).
AMPS_ENTITY = "charging_amps"   # number: charging current setpoint (A)
CHARGER_ENTITY = "charger"      # switch: start/stop charging
ESPHOME_API_PORT = 6053


class SimHttpCar:
    """Talks to sim_server's /_sim/car, standing in for the ESP32."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    async def apply(self, cmd: Command) -> None:
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{self.base_url}/_sim/car",
                                    json={"charging": cmd.charging, "amps": cmd.amps}) as resp:
                resp.raise_for_status()


class EsphomeCar:
    """Drives the car through the ESP32 over the ESPHome native API (encrypted with noise_psk).

    Connects lazily and drops the connection on any error, so the next command reconnects.
    The ESP32 only forwards the command over BLE; it reaches the car only if it is paired and in range.
    """

    def __init__(self, host: str, noise_psk: str):
        self.host, self.noise_psk = host, noise_psk
        self.client: APIClient | None = None
        self.keys: dict[str, int] = {}

    async def _connect(self) -> APIClient:
        if self.client is not None:
            return self.client
        # Stored before connecting so a failed or cancelled (timed-out) attempt is still closed.
        self.client = client = APIClient(self.host, ESPHOME_API_PORT, None, noise_psk=self.noise_psk)
        await client.connect(login=True)
        entities, _services = await client.list_entities_services()
        self.keys = {entity.object_id: entity.key for entity in entities}
        missing = {AMPS_ENTITY, CHARGER_ENTITY} - self.keys.keys()
        if missing:
            raise RuntimeError(f"ESP32 firmware lacks entities {sorted(missing)}")
        return client

    async def apply(self, cmd: Command) -> None:
        try:
            client = await self._connect()
            if cmd.charging:
                # Amps first, so charging never (re)starts at a stale, higher setpoint.
                client.number_command(self.keys[AMPS_ENTITY], float(cmd.amps))
            client.switch_command(self.keys[CHARGER_ENTITY], cmd.charging)
        except BaseException:  # BaseException: also a timeout cancellation from main.py
            await self.close()
            raise

    async def close(self) -> None:
        client, self.client = self.client, None
        if client is None:
            return
        try:
            # force: drop the socket without waiting for a DisconnectResponse, which on a
            # broken link takes another 10 s and would stall the control loop again.
            await client.disconnect(force=True)
        except Exception:  # already broken; nothing left to release
            pass

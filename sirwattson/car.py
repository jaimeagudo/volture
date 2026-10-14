from __future__ import annotations

import aiohttp

from .controller import Command


class SimHttpCar:
    """Talks to sim_server's /_sim/car, standing in for the ESP32."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    async def apply(self, cmd: Command) -> None:
        async with aiohttp.ClientSession() as s:
            await s.post(f"{self.base_url}/_sim/car",
                         json={"charging": cmd.charging, "amps": cmd.amps})


class EsphomeCar:
    """TODO: aioesphomeapi client for the yoziru firmware (entity names after first flash)."""

    def __init__(self, host: str, noise_psk: str):
        raise NotImplementedError("Implement after the ESP32 is flashed and entity IDs are known")

    async def apply(self, cmd: Command) -> None:
        raise NotImplementedError

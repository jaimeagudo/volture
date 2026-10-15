"""Simulated car adapter: same interface as volture.car.EsphomeCar, backed by simulator.server."""
from __future__ import annotations

from typing import Optional

import aiohttp
from volture.confirm import CarState
from volture.controller import Command

HTTP_TIMEOUT = aiohttp.ClientTimeout(total=3)


class SimHttpCar:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def apply(self, cmd: Command) -> None:
        async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
            async with session.post(f"{self.base_url}/_sim/car",
                                    json={"charging": cmd.charging, "amps": cmd.amps}) as resp:
                resp.raise_for_status()

    async def observe(self) -> Optional[CarState]:
        try:
            async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
                async with session.get(f"{self.base_url}/_sim/state") as resp:
                    resp.raise_for_status()
                    state = await resp.json()
            return CarState(bool(state["car_charging"]), int(state["car_amps"]), bool(state["car_plugged"]),
                            float(state["car_battery_pct"]), float(state["car_limit_pct"]))
        except Exception:  # simulator down: the car's state is unknown, like an offline ESP32
            return None

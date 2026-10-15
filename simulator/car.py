"""Simulated car adapter: same interface as volture.car.EsphomeCar, backed by simulator.server."""
from __future__ import annotations

from typing import Optional

import logging

import aiohttp
from volture.confirm import CarState
from volture.controller import Command

HTTP_TIMEOUT = aiohttp.ClientTimeout(total=3)
log = logging.getLogger("volture.sim")


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


class RealCarSeenBySimShelly:
    """The real car (EsphomeCar) with the simulator's fake Shelly: each observed state is copied to
    the simulator's car, so the fake reading is the house power you type + the real car's power,
    like a real clamp on the main feed. Without it the reading ignores the car and Volture ramps blind.
    """

    def __init__(self, car, base_url: str):
        self.car, self.base_url = car, base_url.rstrip("/")

    async def start(self) -> None:
        await self.car.start()

    async def stop(self) -> None:
        await self.car.stop()

    async def apply(self, cmd: Command) -> None:
        await self.car.apply(cmd)

    async def observe(self) -> Optional[CarState]:
        state = await self.car.observe()
        if state is None:
            return None
        try:
            async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
                async with session.post(f"{self.base_url}/_sim/car?force=1",
                                        json={"charging": state.charging, "amps": state.amps}) as resp:
                    resp.raise_for_status()
        except Exception as e:  # the fake reading misses the car this time; the 20 A cap still holds
            log.warning("could not copy the real car's load to the simulator: %r", e)
        return state

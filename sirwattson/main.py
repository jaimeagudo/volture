from __future__ import annotations

import argparse
import asyncio
import logging
import time

import aiohttp
from yarl import URL

from .car import EsphomeCar, SimHttpCar
from .controller import Config, Controller, Reading
from .secrets import read_secret

log = logging.getLogger("sirwattson")
FAKE_SHELLY_URL = "http://localhost:8081"  # sim_server.py default port
# A command must never stall the loop: power readings stop while we wait. Connecting to the
# ESP32 has been seen to hang for 60-90 s; a healthy connect + command takes ~1 s.
CAR_COMMAND_TIMEOUT_S = 10.0


async def read_shelly(session: aiohttp.ClientSession, url: str) -> Reading | None:
    try:
        async with session.get(f"{url}/emeter/0") as r:
            r.raise_for_status()
            d = await r.json()
            return Reading(float(d["power"]), bool(d.get("is_valid", False)))
    except Exception as e:  # network error, bad JSON, HTTP error -> treated as no reading
        log.warning("shelly read failed: %s", e)
        return None


async def run(shelly_url: str, car, poll_s: float) -> None:
    ctrl = Controller(Config())
    timeout = aiohttp.ClientTimeout(total=3)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            reading = await read_shelly(session, shelly_url)
            cmd = ctrl.tick(time.monotonic(), reading)
            log.info("reading=%s -> %s", reading, cmd)
            if cmd:
                try:
                    await asyncio.wait_for(car.apply(cmd), CAR_COMMAND_TIMEOUT_S)
                except Exception as e:
                    # Keep looping: the next ticks re-evaluate power and, if still over the
                    # limit, issue another (lower or stop) command.
                    log.error("car command %s failed: %r", cmd, e)
            await asyncio.sleep(poll_s)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    # Default from secrets.yaml, so switching between fake and real Shelly is one edit there.
    ap.add_argument("--shelly-url", default=read_secret("shelly_url", default=FAKE_SHELLY_URL))
    ap.add_argument("--car", choices=["sim", "esphome"], default="sim")
    ap.add_argument("--sim-url", default=FAKE_SHELLY_URL)
    ap.add_argument("--esphome-host", default="sirwattson.local")
    ap.add_argument("--poll", type=float, default=5.0)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    log.info("reading power from %s", URL(a.shelly_url).with_user(None))  # never log credentials
    if a.car == "sim":
        car = SimHttpCar(a.sim_url)
    else:
        car = EsphomeCar(a.esphome_host, read_secret("api_encryption_key"))
    asyncio.run(run(a.shelly_url, car, a.poll))

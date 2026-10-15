"""Control loop: read the Shelly -> confirm the last command -> decide -> command the car.

    uv run python -m volture.main                           # real Shelly, real car
    uv run python -m volture.main --sim-shelly              # fake Shelly, real car
    uv run python -m volture.main --sim-car                 # real Shelly, simulated car
    uv run python -m volture.main --sim-shelly --sim-car    # everything simulated

Real hardware: `shelly_url` and `volture: esphome_host` in secrets.yaml. Simulated pieces come
from python -m simulator.server, at --sim-url (default http://localhost:8081).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import time
from typing import Optional

import aiohttp
from yarl import URL

from .confirm import Action, CarState, Pending, can_charge, external_change, matches, reconcile
from .controller import Command, Config, Controller, Reading
from .secrets import LoopSettings, read_secret, read_settings

log = logging.getLogger("volture")
SIM_URL = "http://localhost:8081"  # simulator.server default port
SHELLY_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=3)


async def read_shelly(session: aiohttp.ClientSession, url: str) -> Reading | None:
    try:
        async with session.get(f"{url}/emeter/0") as r:
            r.raise_for_status()
            d = await r.json()
            voltage = d.get("voltage")
            return Reading(float(d["power"]), bool(d.get("is_valid", False)),
                           float(voltage) if voltage is not None else None)
    except Exception as e:  # network error, bad JSON, HTTP error -> treated as no reading
        log.warning("shelly read failed: %s", e)
        return None


async def observe(car) -> Optional[CarState]:
    try:
        return await car.observe()
    except Exception as e:
        log.warning("car state unavailable: %r", e)
        return None


async def send(car, cmd: Command, loop: LoopSettings) -> None:
    try:
        # A command must never stall the loop: power readings stop while we wait.
        await asyncio.wait_for(car.apply(cmd), loop.car_command_timeout_s)
    except Exception as e:
        # Keep looping: the command stays pending and is re-sent after confirm_timeout_s.
        log.error("car command %s failed: %r", cmd, e)


async def confirm(ctrl: Controller, car, pending: Optional[Pending], last: Optional[CarState],
                  observed: Optional[CarState], now: float, loop: LoopSettings) -> Optional[Pending]:
    """Follow the car's reported state; re-send or adopt. Returns what stays pending."""
    if external_change(pending, last, observed):
        # Not what Volture asked for: the Tesla app, a schedule or the car itself did it.
        # A start is taken over (amps rise to what fits); a stop stays a stop.
        log.warning("car changed outside Volture to %s: following it", observed)
        ctrl.follow_user(observed.charging, observed.amps)
        return None
    if pending is None:
        if observed is not None and not matches(Command(ctrl.charging, ctrl.amps), observed):
            # First report after startup (or after giving up on a command): align with reality.
            log.warning("car reports %s, controller believed charging=%s amps=%s: adopting",
                        observed, ctrl.charging, ctrl.amps)
            ctrl.adopt(observed.charging, observed.amps)
        return None
    action, still_pending = reconcile(pending, observed, now, loop.confirm_timeout_s,
                                      loop.confirm_max_attempts)
    if action is Action.RESEND:
        log.warning("car did not confirm %s (reports %s): re-sending, attempt %d",
                    pending.cmd, observed, still_pending.attempts)
        await send(car, pending.cmd, loop)
    elif action is Action.ADOPT:
        log.warning("car ignored %s after %d attempts; adopting its state %s",
                    pending.cmd, pending.attempts, observed)
        ctrl.adopt(observed.charging, observed.amps)
    elif action is Action.UNKNOWN:
        log.error("car never confirmed %s and reports no state; keeping our belief", pending.cmd)
    elif action is Action.CONFIRMED:
        log.info("car confirmed %s", pending.cmd)
    return still_pending


async def run(shelly_url: str, car, cfg: Config, loop: LoopSettings) -> None:
    ctrl = Controller(cfg)
    pending: Optional[Pending] = None
    last: Optional[CarState] = None  # previous report, to tell Volture's changes from the app's
    await car.start()
    try:
        async with aiohttp.ClientSession(timeout=SHELLY_HTTP_TIMEOUT) as session:
            while True:
                now = time.monotonic()
                reading = await read_shelly(session, shelly_url)
                observed = await observe(car)
                pending = await confirm(ctrl, car, pending, last, observed, now, loop)
                last = observed if observed is not None else last
                cmd = ctrl.tick(now, reading, can_charge(observed))
                log.info("reading=%s car=%s -> %s", reading, observed, cmd)
                # A repeat of the unconfirmed command is left to confirm(): re-sending it here
                # would restart its attempt count, so the car's state would never be adopted.
                if cmd and (pending is None or cmd != pending.cmd):
                    await send(car, cmd, loop)
                    pending = Pending(cmd, now)
                await asyncio.sleep(loop.poll_s)
    finally:
        await car.stop()


def make_car(sim_car: bool, sim_shelly: bool, sim_url: str, esphome_host: str):
    if sim_car:
        from simulator.car import (
            SimHttpCar,  # simulator code is not needed in production
        )
        return SimHttpCar(sim_url)
    from .car import EsphomeCar
    car = EsphomeCar(esphome_host, read_secret("api_encryption_key"))
    if sim_shelly:
        from simulator.car import RealCarSeenBySimShelly
        return RealCarSeenBySimShelly(car, sim_url)  # the fake Shelly must see the real car's load
    return car


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Keep house + car under the contracted power.")
    ap.add_argument("--sim-shelly", action="store_true", help="read the simulator's fake Shelly")
    ap.add_argument("--sim-car", action="store_true", help="drive the simulator's car, not the real one")
    ap.add_argument("--sim-url", default=SIM_URL, help=f"simulator.server address (default {SIM_URL})")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    config, loop_settings = read_settings()
    shelly_url = a.sim_url if a.sim_shelly else read_secret("shelly_url")
    log.info("reading power from %s (%s)", URL(shelly_url).with_user(None),  # never log credentials
             "simulated" if a.sim_shelly else "real Shelly")
    log.info("car: %s", f"simulated at {a.sim_url}" if a.sim_car else f"real, via {loop_settings.esphome_host}")
    log.info("settings: %s %s", config, loop_settings)
    try:
        car = make_car(a.sim_car, a.sim_shelly, a.sim_url, loop_settings.esphome_host)
        asyncio.run(run(shelly_url, car, config, loop_settings))
    except KeyboardInterrupt:
        pass

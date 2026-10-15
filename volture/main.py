"""Control loop: read the Shelly -> confirm the last command -> decide -> command the car.

    uv run python -m volture.main

Everything comes from secrets.yaml: `shelly_url` (real or fake Shelly) and, in the `volture:`
block, `car: esphome` (real car via the ESP32) or `car: sim` (the car of python -m simulator.server,
served at shelly_url). `--car` overrides the setting for one run.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import time
from dataclasses import replace
from typing import Optional

import aiohttp
from yarl import URL

from .confirm import Action, CarState, Pending, can_charge, external_change, matches, reconcile
from .controller import Command, Config, Controller, Reading
from .secrets import CAR_KINDS, LoopSettings, read_secret, read_settings

log = logging.getLogger("volture")
FAKE_SHELLY_URL = "http://localhost:8081"  # simulator.server default port
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
    if observed is not None and observed.plugged is False and (ctrl.user_paused or ctrl.user_amps):
        log.info("cable unplugged: Tesla app overrides cleared, next session is Volture's")
        ctrl.clear_user()
    if external_change(pending, last, observed):
        # Not what Volture asked for: the Tesla app (or the car itself) did it, and it has priority.
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


def make_car(kind: str, shelly_url: str, esphome_host: str):
    if kind == "sim":
        from simulator.car import (
            SimHttpCar,  # simulator code is not needed in production
        )
        return SimHttpCar(shelly_url)  # the simulator serves the fake Shelly and the fake car
    from .car import EsphomeCar
    return EsphomeCar(esphome_host, read_secret("api_encryption_key"))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    # Default from secrets.yaml, so switching between fake and real Shelly is one edit there.
    ap.add_argument("--shelly-url", default=read_secret("shelly_url", default=FAKE_SHELLY_URL))
    ap.add_argument("--car", choices=CAR_KINDS, help="override `car:` in secrets.yaml for this run")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    config, loop_settings = read_settings()
    if a.car:
        loop_settings = replace(loop_settings, car=a.car)
    log.info("reading power from %s", URL(a.shelly_url).with_user(None))  # never log credentials
    log.info("settings: %s %s", config, loop_settings)
    try:
        car = make_car(loop_settings.car, a.shelly_url, loop_settings.esphome_host)
        asyncio.run(run(a.shelly_url, car, config, loop_settings))
    except KeyboardInterrupt:
        pass

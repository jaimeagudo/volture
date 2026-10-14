"""Fake Shelly EM (Gen1) + fake ESP32 car endpoint, for testing without hardware.

GET  /shelly, /emeter/0          -> Shelly Gen1 style JSON
POST /_sim/car   {charging,amps} -> what the ESP32 would do
POST /_sim/base?w=3500           -> set house base load (W)
POST /_sim/shelly?valid=0|1&online=0|1
GET  /_sim/state
"""
from __future__ import annotations

import argparse
import time

from aiohttp import web

from .controller import Command
from .sim import World

T0 = time.monotonic()


def now() -> float:
    return time.monotonic() - T0


def make_app(world: World) -> web.Application:
    async def shelly(_):
        return web.json_response({"type": "SHEM", "mac": "SIMULATED", "fw": "sim"})

    async def emeter(_):
        if not world.shelly_online:
            raise web.HTTPServiceUnavailable()
        total = world.total_w(now())
        return web.json_response({
            "power": round(total, 1), "reactive": 0.0,
            "voltage": world.voltage_v, "is_valid": world.shelly_valid,
            "total": 0.0, "total_returned": 0.0,
        })

    async def car(request):
        d = await request.json()
        world.car.command(Command(bool(d["charging"]), int(d["amps"])), now())
        return web.json_response({"ok": True})

    async def base(request):
        world.base_w = float(request.query["w"])
        return web.json_response({"base_w": world.base_w})

    async def shelly_mode(request):
        q = request.query
        if "valid" in q:
            world.shelly_valid = q["valid"] == "1"
        if "online" in q:
            world.shelly_online = q["online"] == "1"
        return web.json_response({"valid": world.shelly_valid, "online": world.shelly_online})

    async def state(_):
        total = world.total_w(now())
        return web.json_response({"base_w": world.base_w, "car_charging": world.car.charging,
                                  "car_amps": world.car.amps, "total_w": total})

    app = web.Application()
    app.add_routes([
        web.get("/shelly", shelly), web.get("/emeter/0", emeter),
        web.post("/_sim/car", car), web.post("/_sim/base", base),
        web.post("/_sim/shelly", shelly_mode), web.get("/_sim/state", state),
    ])
    return app


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--base", type=float, default=650.0)
    a = ap.parse_args()
    web.run_app(make_app(World(base_w=a.base)), port=a.port)

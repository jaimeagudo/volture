"""Fake Shelly EM (Gen1) + fake ESP32 car endpoint, for testing without hardware.

Also lets you *be* the Shelly while the real car is driven through the ESP32: open
http://localhost:8081/ (or http://<this-mac-ip>:8081/ from a phone) and type the house power.

GET  /                           -> control page: power, valid, online
GET  /shelly, /emeter/0          -> same JSON fields as a real Shelly EM Gen1
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

# Control page. With --car esphome nothing commands the simulated car, so the power you type
# is exactly what SirWattson reads. With --car sim, the simulated car's load is added on top.
CONTROL_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Fake Shelly</title>
<style>body{font:18px system-ui;margin:16px;max-width:420px}input[type=number]{font-size:24px;width:8ch}
button{font-size:18px;padding:8px 14px;margin:4px 4px 0 0}pre{background:#eee;padding:8px;overflow-x:auto}</style>
</head><body><h1>Fake Shelly EM</h1>
<form id="f"><label>House power (W) <input type="number" id="w" step="50" min="-10000" max="30000"></label>
<button>Set</button></form>
<p><button data-w="650">650 W idle</button><button data-w="3500">3500 W oven</button><button data-w="5000">5000 W over limit</button></p>
<p><label><input type="checkbox" id="valid"> is_valid</label> <label><input type="checkbox" id="online"> online</label></p>
<p>SirWattson reads <code>/emeter/0</code>:</p><pre id="out">...</pre>
<script>
const post = (url) => fetch(url, {method: "POST"}).then(refresh);
const setW = (w) => post("/_sim/base?w=" + Number(w));
document.getElementById("f").onsubmit = (e) => { e.preventDefault(); setW(document.getElementById("w").value); };
document.querySelectorAll("[data-w]").forEach((b) => b.onclick = () => setW(b.dataset.w));
for (const name of ["valid", "online"]) {
  document.getElementById(name).onchange = (e) => post(`/_sim/shelly?${name}=${e.target.checked ? 1 : 0}`);
}
async function refresh() {
  try {
    const st = await (await fetch("/_sim/state")).json();
    document.getElementById("valid").checked = st.shelly_valid;
    document.getElementById("online").checked = st.shelly_online;
    if (document.activeElement.id !== "w") { document.getElementById("w").value = st.base_w; }
    const em = await fetch("/emeter/0");
    document.getElementById("out").textContent = em.ok ? JSON.stringify(await em.json(), null, 1) : "HTTP " + em.status + " (offline)";
  } catch (err) { document.getElementById("out").textContent = "server unreachable"; }
}
refresh(); setInterval(refresh, 1000);
</script></body></html>"""


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
        try:
            world.base_w = float(request.query["w"])
        except (KeyError, ValueError):
            raise web.HTTPBadRequest(text="expected ?w=<watts>")
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
                                  "car_amps": world.car.amps, "total_w": total,
                                  "shelly_valid": world.shelly_valid, "shelly_online": world.shelly_online})

    async def page(_):
        return web.Response(text=CONTROL_PAGE, content_type="text/html")

    app = web.Application()
    app.add_routes([
        web.get("/", page), web.get("/shelly", shelly), web.get("/emeter/0", emeter),
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

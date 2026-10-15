"""Fake Shelly EM (Gen1) + fake ESP32 car endpoint, for testing without hardware.

Also lets you *be* the Shelly while the real car is driven through the ESP32: open
http://localhost:8081/ (or http://<this-mac-ip>:8081/ from a phone) and type the house power.

GET  /                           -> control page: power, voltage, valid, online, car reachable
GET  /shelly, /emeter/0          -> same JSON fields as a real Shelly EM Gen1
POST /_sim/car   {charging,amps} -> what the ESP32 would do (?force=1: act as the Tesla app)
POST /_sim/base?w=3500           -> set house base load (W)
POST /_sim/shelly?valid=0|1&online=0|1&voltage=225
POST /_sim/car_link?reachable=0|1&plugged=0|1&battery=60&limit=80
                                 -> reachable=0: the car silently ignores commands (out of BLE range)
GET  /_sim/state
"""
from __future__ import annotations

import argparse
import time

from aiohttp import web
from volture.controller import Command

from .model import World

T0 = time.monotonic()

# Control page. The simulated car's load is added to the fake Shelly whenever it charges. Without
# volture.main --sim-car nothing starts it, so the power you type is exactly what volture reads.
CONTROL_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>volture simulator</title>
<style>body{font:18px system-ui;margin:16px;max-width:440px}section{border:1px solid #ccc;border-radius:8px;padding:4px 12px;margin:0 0 16px}
h2{font-size:20px;margin:8px 0}input[type=number]{font-size:22px;width:7ch}#b,#l{width:3.5ch}
button{font-size:18px;padding:8px 14px;margin:4px 4px 0 0}pre{background:#eee;padding:8px;overflow-x:auto}</style>
</head><body><h1>volture simulator</h1>
<section><h2>Shelly (house meter)</h2>
<form id="fw"><label>Power (W) <input type="number" id="w" step="50" min="-10000" max="30000"></label> <button>Set</button></form>
<p><button data-w="650">650 W idle</button><button data-w="3500">3500 W oven</button><button data-w="5000">5000 W over limit</button></p>
<form id="fv"><label>Voltage (V) <input type="number" id="v" step="1" min="0" max="300"></label> <button>Set</button></form>
<p><label><input type="checkbox" id="valid"> is_valid</label> <label><input type="checkbox" id="online"> online</label></p>
<p>volture reads <code>/emeter/0</code>:</p><pre id="out">...</pre></section>
<section><h2>Simulated car</h2>
<p>Car: <b id="car">...</b></p>
<p>Act as the Tesla app:</p>
<form id="fa"><label>Amps <input type="number" id="a" value="32" min="1" max="48"></label> <button>Start charging</button></form>
<p><button id="stop">Stop charging</button></p>
<p><label title="Untick: the car silently ignores volture, like out of Bluetooth range"><input type="checkbox" id="reachable"> reachable by volture</label>
<label><input type="checkbox" id="plugged"> plugged in</label></p>
<form id="fb"><label>Battery % <input type="number" id="b" min="0" max="100"></label>
<label>Limit % <input type="number" id="l" min="50" max="100"></label> <button>Set</button></form>
<p><small>Its load is added to the Shelly reading while it charges.</small></p></section>
<script>
const post = (url, body) => fetch(url, {method: "POST", headers: {"content-type": "application/json"},
  body: body === undefined ? undefined : JSON.stringify(body)}).then(refresh);
const val = (id) => Number(document.getElementById(id).value);
const setW = (w) => post("/_sim/base?w=" + Number(w));
document.getElementById("fw").onsubmit = (e) => { e.preventDefault(); setW(val("w")); };
document.getElementById("fv").onsubmit = (e) => { e.preventDefault(); post("/_sim/shelly?voltage=" + val("v")); };
document.querySelectorAll("[data-w]").forEach((b) => b.onclick = () => setW(b.dataset.w));
for (const name of ["valid", "online"]) {
  document.getElementById(name).onchange = (e) => post(`/_sim/shelly?${name}=${e.target.checked ? 1 : 0}`);
}
document.getElementById("fa").onsubmit = (e) => { e.preventDefault(); post("/_sim/car?force=1", {charging: true, amps: val("a")}); };
document.getElementById("stop").onclick = () => post("/_sim/car?force=1", {charging: false, amps: 0});
for (const name of ["reachable", "plugged"]) {
  document.getElementById(name).onchange = (e) => post(`/_sim/car_link?${name}=${e.target.checked ? 1 : 0}`);
}
document.getElementById("fb").onsubmit = (e) => { e.preventDefault(); post(`/_sim/car_link?battery=${val("b")}&limit=${val("l")}`); };
async function refresh() {
  try {
    const st = await (await fetch("/_sim/state")).json();
    document.getElementById("valid").checked = st.shelly_valid;
    document.getElementById("online").checked = st.shelly_online;
    document.getElementById("reachable").checked = st.car_reachable;
    document.getElementById("plugged").checked = st.car_plugged;
    const why = !st.car_plugged ? " (unplugged)" : st.car_battery_pct >= st.car_limit_pct ? " (at charge limit)" : "";
    document.getElementById("car").textContent = st.car_charging ? `charging at ${st.car_amps} A` : "not charging" + why;
    if (!["b", "l"].includes(document.activeElement.id)) {
      document.getElementById("b").value = st.car_battery_pct; document.getElementById("l").value = st.car_limit_pct;
    }
    if (document.activeElement.id !== "w") { document.getElementById("w").value = st.base_w; }
    if (document.activeElement.id !== "v") { document.getElementById("v").value = st.voltage_v; }
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
        try:
            d = await request.json()
            cmd = Command(bool(d["charging"]), int(d["amps"]))
        except (ValueError, KeyError, TypeError):
            raise web.HTTPBadRequest(text='expected {"charging": bool, "amps": int}')
        if request.query.get("force") == "1":
            world.car.force(cmd)  # the page acting as the Tesla app: immediate, always obeyed
        else:
            world.car.command(cmd, now())  # volture's command: delayed, lost if unreachable
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
        if "voltage" in q:
            try:
                world.voltage_v = float(q["voltage"])
            except ValueError:
                raise web.HTTPBadRequest(text="expected ?voltage=<volts>")
        return web.json_response({"valid": world.shelly_valid, "online": world.shelly_online,
                                  "voltage_v": world.voltage_v})

    async def car_link(request):
        q, car = request.query, world.car
        try:
            if "reachable" in q:
                car.reachable = q["reachable"] == "1"
            if "plugged" in q:
                car.plugged = q["plugged"] == "1"
            if "battery" in q:
                car.battery_pct = float(q["battery"])
            if "limit" in q:
                car.limit_pct = float(q["limit"])
        except ValueError:
            raise web.HTTPBadRequest(text="expected numbers for battery / limit")
        car.settle()
        return web.json_response({"car_reachable": car.reachable, "car_plugged": car.plugged,
                                  "car_battery_pct": car.battery_pct, "car_limit_pct": car.limit_pct})

    async def state(_):
        total = world.total_w(now())
        return web.json_response({"base_w": world.base_w, "car_charging": world.car.charging,
                                  "car_amps": world.car.amps, "total_w": total,
                                  "shelly_valid": world.shelly_valid, "shelly_online": world.shelly_online,
                                  "voltage_v": world.voltage_v, "car_reachable": world.car.reachable,
                                  "car_plugged": world.car.plugged, "car_battery_pct": world.car.battery_pct,
                                  "car_limit_pct": world.car.limit_pct})

    async def page(_):
        return web.Response(text=CONTROL_PAGE, content_type="text/html")

    app = web.Application()
    app.add_routes([
        web.get("/", page), web.get("/shelly", shelly), web.get("/emeter/0", emeter),
        web.post("/_sim/car", car), web.post("/_sim/base", base),
        web.post("/_sim/shelly", shelly_mode), web.post("/_sim/car_link", car_link),
        web.get("/_sim/state", state),
    ])
    return app


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--base", type=float, default=650.0)
    a = ap.parse_args()
    web.run_app(make_app(World(base_w=a.base)), port=a.port)

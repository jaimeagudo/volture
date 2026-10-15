# SirWattson

**Charge a Tesla Model 3 at home as fast as possible without ever tripping the house's 4.6 kW contracted power limit.**

A **Shelly EM** clamp on the main feed measures what the whole house draws. A small Python program reads it every 5 s and decides how many amps (5–32 A) the car may use right now. It sends that number to an **ESP32**, which talks to the car over Bluetooth Low Energy (BLE). When the oven turns on, the car slows down; when it turns off, the car speeds up again.

> **Safety:** this is a load *manager*, not a protection device. It does not replace the utility's power limiter or the breakers. Every failure mode is designed to end in "charge less" or "stop charging" (see [Fail-safe](#fail-safe)).

## Status (October 2026)

| Piece | State |
|---|---|
| Control algorithm + tests | Done, 16 tests pass; every setting in `secrets.yaml` |
| Simulator (fake Shelly + fake car, web page) | Done, run end to end |
| ESP32 firmware | Built from `esp32.yaml`, flashed, on Wi-Fi as `sirwattson.local` |
| Mac → ESP32 commands (`EsphomeCar`) | Done; ESP32 confirmed receiving "set amps" and "charging on/off". Its Wi-Fi is weak where it sits now (-86 dBm): see Troubleshooting |
| ESP32 → car | **Next:** pair the ESP32 with the car (`python -m sirwattson.pair`, sitting in the car), then check that the car's reported state confirms each command |
| Shelly EM | **Waiting for delivery** |

---

## How it works

```text
          main feed (230 V, single phase)
                    |
            [clamp] --> Shelly EM (Gen1) ---Wi-Fi, HTTP GET /emeter/0 every 5 s---+
                                                                                   v
   +-------------------------------------------------------------------------------+
   |  SirWattson (Python, on a Mac / Raspberry Pi)                                 |
   |  read Shelly + car state -> controller.py (pure) -> car.py (send, confirm)    |
   +-------------------------------------------------------------------------------+
                                  | ESPHome native API, encrypted, Wi-Fi
                                  v
                    ESP32 running esp32.yaml (yoziru/esphome-tesla-ble firmware)
                                  | Bluetooth Low Energy
                                  v
                            Tesla Model 3
```

| Piece | Job |
|---|---|
| Shelly EM Gen1 + clamp | Measures total house power, car included (the clamp is on the main feed). |
| ESP32 (Elegoo, classic ESP32-D0WD-V3, 4 MB flash, CP2102 USB chip) | Runs third-party firmware that speaks Tesla's BLE protocol: set amps, start/stop charging. |
| SirWattson (this repo) | Reads the Shelly, decides the amps, tells the ESP32. No Home Assistant. |
| Simulator | Fake Shelly and fake car, to test everything without hardware. |

**Design rule:** `controller.py` and `confirm.py` are pure: (time, reading, local hour, car state) in, command out. No network, no clock. Everything that touches the outside world sits behind small adapters (`car.py`, `read_shelly`), so the algorithm is tested deterministically and hardware can be swapped without touching it.

---

## Control algorithm

Runs every `poll_s` with the Shelly reading (`power`, `voltage`, `is_valid`). The numbers below are the defaults; **every one is a setting in the `sirwattson:` block of `secrets.yaml`**, so another installation only needs that file edited. Ceiling = `contracted_power_w` − `headroom_w` = 4,600 − 230 = **4,370 W**. V is the voltage the Shelly measures (`fallback_voltage_v`, 230 V, if it reports none within 180–260 V).

| Situation | Action |
|---|---|
| Outside the off-peak window (if set) | Stop, and do not restart until the window opens |
| Charging and power > 4,370 W | Drop ⌈(P − 4,370) / V⌉ A at once, minimum `min_amps` (5) |
| Already at 5 A and power > 4,600 W | Stop charging |
| Charging and power + `ramp_up_step_amps` × V ≤ 4,370 W | Raise `ramp_up_step_amps` (1) A, at most every `ramp_up_every_s` (15 s), up to `max_amps` (32) |
| Otherwise while charging | Hold |
| Stopped and house + 5 A × V fits under 4,370 W for `resume_after_s` (60 s) | Restart at 5 A |
| Believed stopped but power > 4,600 W | Send "stop" (at most every 15 s): the car may be charging without us |
| Shelly unreachable or `is_valid: false` | Drop to 5 A; after `stop_after_invalid_s` (30 s), stop |

**Off-peak window:** `offpeak_start_hour` / `offpeak_end_hour` are whole hours in `timezone` (Europe/Madrid, summer time handled). The window may cross midnight (22 → 7). Unset: charge any time. Weekends are not special-cased.

**Following the car:** SirWattson listens to the car's reported state (the ESP32's `charger` switch and `charging_amps`).
- After each command it waits `confirm_timeout_s` (15 s) for the car to report it. If not, it re-sends, up to `confirm_max_attempts` (3) sends in total; after that it adopts what the car reports.
- If the car changes on its own (Tesla app, cable unplugged), the controller adopts that state and adapts. For example, a 32 A charge started from the app is cut to what fits.
- `last_command` (the firmware's text outcome) is only logged.

### Fail-safe

- **No trustworthy reading → charge less, then stop.** The controller never keeps commanding power blind.
- **A failed or unconfirmed command is logged and re-sent; the loop never stops reading power.** A command gives up after `car_command_timeout_s` (10 s), and the ESP32 connection is kept alive in the background.
- **A stop from the Tesla app is not permanent:** once power has been low for `resume_after_s`, SirWattson restarts charging (inside the off-peak window).

---

## Repository layout

```text
SirWattson/
├── esp32.yaml            # ESP32 firmware config (pulls yoziru/esphome-tesla-ble, pinned release)
├── secrets.example.yaml  # Template for secrets.yaml (gitignored), shared by firmware and Python
├── sirwattson/           # What runs for real
│   ├── controller.py     # Pure control logic + its settings (Config)
│   ├── confirm.py        # Pure logic: did the car obey the last command?
│   ├── car.py            # EsphomeCar: the real car through the ESP32
│   ├── main.py           # Loop: read Shelly -> confirm -> decide -> command car
│   ├── pair.py           # One-off: pair the ESP32's key with the car
│   └── secrets.py        # Reads secrets.yaml (secrets + the sirwattson: settings block)
├── simulator/            # Testing without hardware, never needed for real
│   ├── model.py          # Model 3 + house model
│   ├── server.py         # Fake Shelly Gen1 + fake car + control page (http://localhost:8081/)
│   └── car.py            # SimHttpCar: the simulated car, same interface as EsphomeCar
├── tests/test_controller.py
└── pyproject.toml
```

---

## Setup guide (from zero, macOS)

Commands go in the **Terminal** app (`Cmd + Space`, type "Terminal", Enter). Copy each block and press Enter.

**You do not need to install ESP-IDF or the ESP-IDF Installation Manager.** ESPHome downloads the whole ESP32 toolchain by itself on the first build.

### 1. Install the tools

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"   # Homebrew
brew install git uv
```

`uv` installs Python and every dependency, ESPHome included.

### 2. Get the code and run the tests

```bash
git clone https://github.com/jaimeagudo/sirwattson.git ~/github/SirWattson
cd ~/github/SirWattson
uv sync
uv run python -m unittest discover -s tests -v
```

All 16 tests must pass. Python 3.12–3.14 works.

### 3. Try the simulator (no hardware)

Terminal 1, the fake Shelly and car:
```bash
uv run python -m simulator.server
```

Terminal 2, the controller with the simulated car (`--car sim` is only needed until `secrets.yaml` says `car: sim`, step 4):
```bash
uv run python -m sirwattson.main --car sim
```

After 60 s charging starts at 5 A, then rises 1 A every 15 s.

Open **http://localhost:8081/** (from a phone: `http://<your-Mac's-IP>:8081/`, Mac IP: `ipconfig getifaddr en0`). Two sections:
- **Shelly (house meter):** house power, presets, voltage, `is_valid`, `online`, and the exact `/emeter/0` JSON SirWattson reads.
- **Simulated car:** its state; start or stop charging as if from the Tesla app (SirWattson must notice and adapt); untick *reachable* to make the car silently ignore SirWattson's commands and watch the re-sends in the controller log.

The same controls with `curl`:

```bash
curl -X POST "localhost:8081/_sim/base?w=3500"          # house power 3500 W
curl -X POST "localhost:8081/_sim/shelly?voltage=215"   # measured voltage
curl -X POST "localhost:8081/_sim/shelly?valid=0"       # Shelly reports invalid readings
curl -X POST "localhost:8081/_sim/shelly?online=0"      # Shelly stops answering (HTTP 503)
curl -X POST "localhost:8081/_sim/car_link?reachable=0" # car ignores SirWattson
curl -X POST "localhost:8081/_sim/car?force=1" -d '{"charging": true, "amps": 32}'  # Tesla app starts 32 A
```

Stop any program with `Ctrl + C`.

#### Be the Shelly while driving the real car

Before the Shelly is installed, you can debug with the **real ESP32 and car**: keep `shelly_url` in `secrets.yaml` pointing at the fake Shelly, set `car: esphome`, run `simulator.server`, then `sirwattson.main`. Nothing commands the simulated car in this mode, so the power you type is exactly what SirWattson reads; the real car's own load is not added, so type the total you want it to see.

### 4. Fill in the secrets

```bash
cp secrets.example.yaml secrets.yaml && open -e secrets.yaml
```

| Key | What it is |
|---|---|
| `wifi_ssid`, `wifi_password` | Your Wi-Fi. It must offer **2.4 GHz**; the ESP32 cannot use 5 GHz. |
| `wifi2_ssid`, `wifi2_password` | A second Wi-Fi (e.g. office and garage). The ESP32 joins whichever is in range. Both are required. |
| `wifi_hotspot_password` | 8+ characters. The ESP32 opens its own Wi-Fi with it if it cannot join yours. |
| `api_encryption_key` | Encrypts Mac ↔ ESP32 traffic. Generate it with the command in the template. |
| `tesla_vin` | The car's VIN (17 characters, in the Tesla app). |
| `ble_mac_address` | The car's Bluetooth **MAC address**, `AA:BB:CC:DD:EE:FF` (digits 0–9 and letters A–F only, so `0` is zero, never the letter `O`). See step 6. |
| `shelly_url` | Where SirWattson reads power: `http://localhost:8081` (fake Shelly) or `http://SHELLY_IP` (real one). |
| `sirwattson:` block | Every setting: `car: esphome` (real) or `sim`, `esphome_host`, the power limits, amps, steps, timings, off-peak hours and time zone. Each line is commented in the template; a missing line keeps its default; a typo or impossible value stops SirWattson at startup with a clear message. |

`secrets.yaml` is gitignored: it never reaches GitHub.

### 5. Flash the ESP32 (first time, over USB)

1. Plug the ESP32 in with a **data** USB cable (many cables only charge).
2. Check the Mac sees it:
   ```bash
   ls /dev/cu.usbserial*
   ```
   Expect `/dev/cu.usbserial-0001`. Nothing? Try another cable or port, then install Silicon Labs' **CP210x** driver.
3. Build and flash (the first build downloads the toolchain and takes a few minutes):
   ```bash
   uv run esphome run esp32.yaml --device /dev/cu.usbserial-0001
   ```
   It ends by streaming the ESP32 log. `wifi: Connected` means it is on your network. `Ctrl + C` to leave.

Later updates go over Wi-Fi: `uv run esphome run esp32.yaml --device sirwattson.local`.

### 6. Find the car's Bluetooth MAC address

The car constantly advertises a Bluetooth name derived from the VIN: `S` + 16 hex characters + `C`. You need the **MAC address** of that advertisement, not the name.

- **Android (easiest):** install **nRF Connect**, scan next to the car, find the 18-character `S…C` name, copy the MAC shown under it.
- **No Android** (iPhones hide MAC addresses): in `esp32.yaml`, uncomment the `tesla_ble_listener` block, flash, take the ESP32 next to the car and watch the log for `Found Tesla vehicle | Name: … | MAC: …`. Put the MAC in `secrets.yaml`, comment the block again and flash again.

### 7. Pair the ESP32 with the car (in the car)

1. Sit in the car with the ESP32 powered and within Bluetooth range, and the Mac on the same Wi-Fi.
2. Run:
   ```bash
   uv run python -m sirwattson.pair
   ```
3. **Immediately** tap the NFC key card on the centre console and confirm the prompt on the car's screen.
4. Check *Controls → Locks*: a new key ("Unknown device") appears. Rename it to "SirWattson" if you like.

The key is paired with the **`CHARGING_MANAGER`** role (set in `esp32.yaml`): it can start/stop charging and set amps, but cannot unlock or drive. Changing the role requires pairing again.

### 8. Install the Shelly EM

1. Power the Shelly, join its Wi-Fi `shellyem-XXXXXXXXXXXX`, open `http://192.168.33.1/` and enter your Wi-Fi.
2. Reserve a **fixed IP** for it in your router; update its firmware.
3. Put the clamp around **one single conductor** of the main feed (not phase and neutral together: they cancel out). **If you are not comfortable working in the electrical panel, have an electrician do it.**
4. Check: `curl http://SHELLY_IP/emeter/0` returns `power`, `voltage`, `is_valid`. Switch on a known load (a heater): `power` must rise by that amount and be **positive**. Negative means the clamp is backwards.

### 9. Run for real

In `secrets.yaml` set `shelly_url: "http://SHELLY_IP"` and `car: esphome`, then:

```bash
uv run python -m sirwattson.main
```

Everything comes from `secrets.yaml`; `--shelly-url` and `--car` override it for one run.

### Troubleshooting

| Symptom | Likely cause |
|---|---|
| No `/dev/cu.usbserial*` | Charge-only cable, bad port, or missing CP210x driver |
| ESP32 log: `wifi … Auth Expired` once, then `Connected` | Normal on some routers: it retries |
| ESP32 never connects to Wi-Fi | Wrong password, or the network is 5 GHz only |
| `Invalid encryption key` | `api_encryption_key` differs from the one the ESP32 was flashed with: flash again |
| `Timeout while resolving IP address for sirwattson.local` | The Mac has not discovered the new name yet; retry, or set `esphome_host` to the ESP32's IP |
| `ESP32 not connected` / `car state unavailable` all the time, though `ping sirwattson.local` answers | Weak ESP32 Wi-Fi: small packets pass, large ones are lost (check `ping -s 1400 sirwattson.local`). Move the ESP32 or the router closer; aim for a `WiFi Signal` above about -75 dBm |
| ESP32 log: `Command failed: … Connection lost` | Car out of Bluetooth range, asleep, or not paired yet |
| Pairing: no prompt on the car | Wrong MAC/VIN in `secrets.yaml`, or the NFC card was not tapped right away |
| Upload ends with `Finishing update failed` | Firmware built for another board; this board needs `tesla-ble-esp32-generic` |
| Controller drops to 5 A then stops | Shelly unreachable or `is_valid: false` (intended fail-safe) |

---

## Decisions

1. **Shelly EM Gen1 (€37) over Gen3 (€60).** Power + `is_valid` over a plain HTTP GET is all that's needed; on-device history is irrelevant because decisions happen every few seconds. Accepted risk: Gen1 firmware is frozen at 1.14.0 (2023) and only gets fixes.
2. **The Shelly cannot replace the ESP32.** Shelly scripts can only scan BLE adverts (no GATT connection) and have ~25 KB of memory; Tesla's BLE protocol needs elliptic-curve crypto and protobuf.
3. **One brain, outside the ESP32, no Home Assistant.** The ESP32 just relays commands; all logic is in testable Python.
4. **ESPHome native API instead of its REST web server.** The native API is encrypted with a key and pushes state changes; REST has no authentication by default and would expose car control to the whole LAN.
5. **Firmware from `esp32.yaml`, not a local clone.** `esp32.yaml` pulls the yoziru firmware from GitHub at a pinned release. No clone, no Makefile; upgrading is changing `tesla_ble_ref` (and the matching `esphome` version in `pyproject.toml`).
6. **Least-privileged car key** (`CHARGING_MANAGER`): a leaked key can only affect charging.
7. **One file to configure everything** (`secrets.yaml`): secrets shared by firmware and Python, plus every algorithm setting in its `sirwattson:` block.
8. **Board:** classic ESP32 → `tesla-ble-esp32-generic`. Alternatives (M5Stack NanoC6, Seeed XIAO ESP32-C6) only matter if Bluetooth range in the garage turns out poor (external antenna).
9. **Restart threshold:** restart only if *house + 5 A* fits under the ceiling (house < 3,220 W); restarting below 3,910 W, as first proposed, would exceed the contracted power at 5 A.
10. **Proportional drop:** one step of ⌈(P − 4,370) / V⌉ A corrects a big spike at once instead of 1 A per cycle.
11. **Confirm with the car's state, not `last_command` text:** the switch/amps states are structured; `last_command` is firmware prose that may change between releases.

---

## To do

- Pair with the car and run a real charge (step 7), then with the Shelly (steps 8–9). Check in the log that the car's reported state confirms each command (`car confirmed …`).
- Improve the ESP32's Wi-Fi signal (-86 dBm where it sits now).
- Measure how often the Shelly Gen1 refreshes `power` (`curl` every second); it bounds how short `poll_s` can be.
- Confirm the clamp model (50 A or 120 A) covers the house's peak. The installation is single-phase.
- Run as a service (`launchd` on macOS, `systemd` on a Raspberry Pi) so it survives reboots.

## Possible improvements

Solar-surplus mode · history and charts (CSV/Prometheus/Grafana) · three-phase support (Shelly 3EM) · weekend / per-day off-peak rules · failure notifications · external-antenna board if BLE range is poor.

---

## Links

- [yoziru/esphome-tesla-ble](https://github.com/yoziru/esphome-tesla-ble): the ESP32 firmware this project runs
- [Shelly Gen1 API](https://shelly-api-docs.shelly.cloud/gen1/) · [Shelly EM](https://kb.shelly.cloud/knowledge-base/shelly-em)
- [aioesphomeapi](https://github.com/esphome/aioesphomeapi): Python client for the ESPHome native API
- [ESPHome native API](https://esphome.io/components/api/) · [uv](https://docs.astral.sh/uv/)

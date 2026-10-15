# SirWattson

**Charge a Tesla Model 3 at home as fast as possible without ever tripping the house's 4.6 kW contracted power limit.**

A **Shelly EM** clamp on the main feed measures what the whole house draws. A small Python program reads it every 5 s and decides how many amps (5–32 A) the car may use right now. It sends that number to an **ESP32**, which talks to the car over Bluetooth Low Energy (BLE). When the oven turns on, the car slows down; when it turns off, the car speeds up again.

> **Safety:** this is a load *manager*, not a protection device. It does not replace the utility's power limiter or the breakers. Every failure mode is designed to end in "charge less" or "stop charging" (see [Fail-safe](#fail-safe)).

## Status (October 2026)

| Piece | State |
|---|---|
| Control algorithm + tests | Done, 7 tests pass |
| Simulator (fake Shelly + fake car) | Done, run end to end |
| ESP32 firmware | Built from `esp32.yaml`, flashed, on Wi-Fi as `sirwattson.local` |
| Mac → ESP32 commands (`EsphomeCar`) | Done; ESP32 confirmed receiving "set amps" and "charging on/off" |
| ESP32 → car | **Next:** pair the ESP32 with the car (`python -m sirwattson.pair`, sitting in the car) |
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
   |  read Shelly  ->  controller.py (pure decision)  ->  car.py (send command)    |
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

**Design rule:** `controller.py` is pure: (time, reading) in, command out. No network, no clock. Everything that touches the outside world sits behind small adapters (`car.py`, `read_shelly`), so the algorithm is tested deterministically and hardware can be swapped without touching it.

---

## Control algorithm

Runs every 5 s with the Shelly reading (`power`, `is_valid`). Limits: contracted power **4,600 W** (20 A × 230 V); working ceiling **4,370 W** (19 A), leaving ~230 W of headroom for loads that switch on between readings.

| Situation | Action |
|---|---|
| Charging and power > 4,370 W | Drop ⌈(P − 4,370) / 230⌉ A at once, minimum 5 A |
| Already at 5 A and power > 4,600 W | Stop charging |
| Charging and power ≤ 4,140 W | Raise 1 A, at most every 15 s, up to 32 A |
| Between 4,140 and 4,370 W | Hold |
| Stopped and house + 5 A fits under 4,370 W for 60 s | Restart at 5 A |
| Believed stopped but power > 4,600 W | Send "stop" (at most every 15 s): the car may be charging without us, e.g. started from the Tesla app |
| Shelly unreachable or `is_valid: false` | Drop to 5 A; after 30 s, stop |

### Fail-safe

- **No trustworthy reading → charge less, then stop.** The controller never keeps commanding power blind.
- **A failed command to the car is logged and the loop continues.** The next readings still see the real power and issue further (lower or stop) commands.
- **Known gap:** commands are fire-and-forget. The ESP32 reports the outcome in its `last_command` entity, but SirWattson does not read it yet (see [To do](#to-do)).

---

## Repository layout

```text
SirWattson/
├── esp32.yaml            # ESP32 firmware config (pulls yoziru/esphome-tesla-ble, pinned release)
├── secrets.example.yaml  # Template for secrets.yaml (gitignored), shared by firmware and Python
├── sirwattson/
│   ├── controller.py     # Pure control logic
│   ├── car.py            # SimHttpCar (simulator) and EsphomeCar (real ESP32)
│   ├── main.py           # Loop: read Shelly -> decide -> command car
│   ├── pair.py           # One-off: pair the ESP32's key with the car
│   ├── secrets.py        # Reads secrets.yaml
│   ├── sim.py            # Model 3 + house model
│   └── sim_server.py     # Fake Shelly Gen1 + fake car HTTP endpoints
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

All 7 tests must pass. Python 3.12–3.14 works.

### 3. Try the simulator (no hardware)

Terminal 1, the fake Shelly and car:
```bash
uv run python -m sirwattson.sim_server
```

Terminal 2, the controller:
```bash
uv run python -m sirwattson.main
```

After 60 s charging starts at 5 A, then rises 1 A every 15 s.

Open **http://localhost:8081/** to play the Shelly: type the house power, use the presets, or untick `is_valid` / `online`. The page shows the exact `/emeter/0` JSON SirWattson reads. From a phone on the same Wi-Fi, use `http://<your-Mac's-IP>:8081/` (Mac IP: `ipconfig getifaddr en0`). The same controls as `curl`:

```bash
curl -X POST "localhost:8081/_sim/base?w=3500"      # house power 3500 W
curl -X POST "localhost:8081/_sim/shelly?valid=0"   # Shelly reports invalid readings
curl -X POST "localhost:8081/_sim/shelly?online=0"  # Shelly stops answering (HTTP 503)
```

Stop any program with `Ctrl + C`.

#### Be the Shelly while driving the real car

Before the Shelly is installed, you can debug with the **real ESP32 and car**: keep `shelly_url` in `secrets.yaml` pointing at the fake Shelly, run `sirwattson.sim_server`, then `sirwattson.main --car esphome`. Nothing commands the simulated car in this mode, so the power you type is exactly what SirWattson reads; the real car's own load is not added, so type the total you want it to see.

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

Set `shelly_url: "http://SHELLY_IP"` in `secrets.yaml`, then:

```bash
uv run python -m sirwattson.main --car esphome
```

The ESP32 address defaults to `sirwattson.local`; the encryption key and Shelly URL are read from `secrets.yaml`. `--shelly-url` overrides it for one run.

### Troubleshooting

| Symptom | Likely cause |
|---|---|
| No `/dev/cu.usbserial*` | Charge-only cable, bad port, or missing CP210x driver |
| ESP32 log: `wifi … Auth Expired` once, then `Connected` | Normal on some routers: it retries |
| ESP32 never connects to Wi-Fi | Wrong password, or the network is 5 GHz only |
| `Invalid encryption key` | `api_encryption_key` differs from the one the ESP32 was flashed with: flash again |
| `Timeout while resolving IP address for sirwattson.local` | The Mac has not discovered the new name yet; retry, or pass `--esphome-host <IP>` |
| `car command … failed: TimeoutError()` every time, though `ping sirwattson.local` answers | Weak ESP32 Wi-Fi: small packets pass, large ones are lost (check `ping -s 1400 sirwattson.local`). Move the ESP32 or the router closer; aim for a `WiFi Signal` above about -75 dBm |
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
7. **One secrets file** shared by firmware and Python, with the key names the firmware requires.
8. **Board:** classic ESP32 → `tesla-ble-esp32-generic`. Alternatives (M5Stack NanoC6, Seeed XIAO ESP32-C6) only matter if Bluetooth range in the garage turns out poor (external antenna).
9. **Restart threshold:** restart only if *house + 5 A* fits under the ceiling (house < 3,220 W); restarting below 3,910 W, as first proposed, would exceed the contracted power at 5 A.
10. **Proportional drop:** one step of ⌈(P − 4,370) / 230⌉ A corrects a big spike at once instead of 1 A per cycle.

---

## To do

- Pair with the car and run a real charge (step 7), then with the Shelly (steps 8–9).
- Read the ESP32's `last_command` / `charger` state so the controller knows when a command did not reach the car.
- Measure how often the Shelly Gen1 refreshes `power` (`curl` every second); it bounds how short the 5 s cycle can be.
- Confirm the installation is single-phase 230 V and the clamp model (50 A or 120 A) covers the house's peak.
- Run as a service (`launchd` on macOS, `systemd` on a Raspberry Pi) so it survives reboots.

## Possible improvements

Solar-surplus mode · history and charts (CSV/Prometheus/Grafana) · use the Shelly's measured voltage instead of a fixed 230 V · three-phase support (Shelly 3EM) · off-peak scheduling · failure notifications · external-antenna board if BLE range is poor.

---

## Links

- [yoziru/esphome-tesla-ble](https://github.com/yoziru/esphome-tesla-ble): the ESP32 firmware this project runs
- [Shelly Gen1 API](https://shelly-api-docs.shelly.cloud/gen1/) · [Shelly EM](https://kb.shelly.cloud/knowledge-base/shelly-em)
- [aioesphomeapi](https://github.com/esphome/aioesphomeapi): Python client for the ESPHome native API
- [ESPHome native API](https://esphome.io/components/api/) · [uv](https://docs.astral.sh/uv/)

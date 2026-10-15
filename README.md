# volture

![Logo](./volture-logo.svg "Logo")

**Charge a Tesla Model 3 at home as fast as possible without ever tripping the house's 4.6 kW contracted power limit.**

A **Shelly EM** clamp on the main feed measures what the whole house draws. Every 5 s, Volture reads it and decides how many amps (5–32 A) the car may use right now. An **ESP32** passes that to the car over Bluetooth Low Energy (BLE). When the oven turns on, the car slows down; when it turns off, the car speeds up again.

**Target:** the ESP32 does everything on its own, 24/7: read the Shelly, decide, command the car. **Today** the C++ loop runs on the ESP32 but only observes ("Volture control" off: it shows what it would do and sends nothing); the Python loop on a Mac drives the car. Turning "Volture control" on, with the Mac loop stopped, hands control to the ESP32.

> **Safety:** this is a load *manager*, not a protection device. It does not replace the utility's power limiter or the breakers. Every failure mode is designed to end in "charge less" or "stop charging" (see [Fail-safe](#fail-safe)).



## Status (October 2026)

| Piece                                        | State                                                                                                                                                |
| -------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| Control algorithm + tests                    | Done in Python and in C++ (`esp32/controller.h`); the same 24 scenarios pass on both, plus 6 for the C++ cycle on its own; every setting in `secrets.yaml`                               |
| Control loop running on the ESP32 itself     | Running, **observe-only by default** (switch "Volture control" on its web page). Decisions match the Mac's on the same reading. Settings: built-in defaults for now |
| Observing the ESP32 (web page, live JSON)     | Done: `http://volture.local` (see [Watching the ESP32](#watching-the-esp32))                                                                         |
| Simulator (fake Shelly + fake car, web page) | Done, run end to end                                                                                                                                 |
| ESP32 firmware                               | Built from `esp32.yaml`, flashed, on Wi-Fi as `volture.local`                                                                                        |
| Mac → ESP32 commands (`EsphomeCar`)          | Done; ESP32 confirmed receiving "set amps" and "charging on/off"                                                                                     |
| ESP32 → car                                  | Paired (2026-10-15). First real charge driven by the Mac loop with a simulated Shelly: amps ramp, every command confirmed by the car                 |
| Shelly EM                                    | **Waiting for delivery**                                                                                                                             |

---

## How it works

Today (the Mac's role moves into the ESP32 next):

```text
          main feed (230 V, single phase)
                    |
            [clamp] --> Shelly EM (Gen1) ---Wi-Fi, HTTP GET /emeter/0 every 5 s---+
                                                                                   v
   +-------------------------------------------------------------------------------+
   |  volture (Python, on a Mac)                                                   |
   |  read Shelly + car state -> controller.py (pure) -> car.py (send, confirm)    |
   +-------------------------------------------------------------------------------+
                                  | ESPHome native API, encrypted, Wi-Fi
                                  v
                    ESP32 running esp32.yaml (yoziru/esphome-tesla-ble firmware)
                                  | Bluetooth Low Energy
                                  v
                            Tesla Model 3
```

| Piece                                                              | Job                                                                                        |
| ------------------------------------------------------------------ | ------------------------------------------------------------------------------------------ |
| Shelly EM Gen1 + clamp                                             | Measures total house power, car included (the clamp is on the main feed).                  |
| ESP32 (Elegoo, classic ESP32-D0WD-V3, 4 MB flash, CP2102 USB chip) | Runs third-party firmware that speaks Tesla's BLE protocol: set amps, start/stop charging. |
| volture (this repo)                                                | Reads the Shelly, decides the amps, tells the ESP32. No Home Assistant.                    |
| `esp32/controller.h`                                               | The same decision logic in C++, to run on the ESP32 itself (planned).                      |
| Simulator                                                          | Fake Shelly and fake car, to test everything without hardware.                             |

**Design rule:** `controller.py`, `confirm.py` and their C++ copy `esp32/controller.h` are pure: (time, reading, car state) in, command out. No network, no clock. Everything that touches the outside world sits behind small adapters (`car.py`, `read_shelly`), so the algorithm is tested deterministically and hardware can be swapped without touching it.

---

## Control algorithm

Runs every `poll_s` with the Shelly reading (`power`, `voltage`, `is_valid`). The numbers below are the defaults; **every one is a setting in the `volture:` block of `secrets.yaml`**, so another installation only needs that file edited. Ceiling = `contracted_power_w` − `headroom_w` = 4,600 − 230 = **4,370 W**. V is the voltage the Shelly measures (`fallback_voltage_v`, 230 V, if it reports none within 180–260 V).

| Situation                                                                  | Action                                                                                         |
| -------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| Charging and power > 4,370 W                                               | Drop ⌈(P − 4,370) / V⌉ A at once, minimum `min_amps` (5)                                       |
| Already at 5 A and power > 4,600 W                                         | Stop charging                                                                                  |
| Charging and power + `ramp_up_step_amps` × V ≤ 4,370 W                     | Raise `ramp_up_step_amps` (1) A, at most every `ramp_up_every_s` (15 s), up to the amps cap   |
| Always, whatever the Shelly says                                           | **Amps cap** = the lower of `max_amps` (32) and ⌊4,600 / V⌋ (**20 A** at 230 V): the car alone never exceeds the contracted power |
| Otherwise while charging                                                   | Hold                                                                                           |
| Stopped **by Volture** and house + 5 A × V fits under 4,370 W for `resume_after_s` (60 s) | Restart at 5 A, unless unplugged or at its battery charge limit                   |
| Stopped by anyone else, or never started                                   | Nothing: **Volture never starts a charge**                                                     |
| Believed stopped but power > 4,600 W                                       | Send "stop" (at most every 15 s): the car may be charging without us                           |
| Shelly unreachable or `is_valid: false`                                    | Drop to 5 A; after `stop_after_invalid_s` (30 s), stop                                         |


**You start and stop; Volture sets the amps.** Charging is always started by you: from the Tesla app, or by the car's own schedule. Volture listens to the car's reported state through the ESP32 (charging on/off, amps, cable connected, battery % and charge limit %). A change it did not ask for came from outside, and:

| Outside change                                                          | What Volture does                                                         |
| ----------------------------------------------------------------------- | ------------------------------------------------------------------------- |
| Charging started (app, schedule), or amps changed in the app            | Takes over: raises the amps to everything that fits under the contracted power |
| Charging stopped (app, schedule, or the car reached its charge limit)   | Stays stopped                                                             |

The house's contracted power wins over the app: a 32 A charge started from the app is cut to what fits under 4,600 W.

When Volture restarts a charge it stopped itself, it skips it while the car is unplugged or its battery is at or above its charge limit; the car would refuse anyway.

- **Confirming commands:** after each command Volture waits `confirm_timeout_s` (15 s) for the car to report it. If not, it re-sends, up to `confirm_max_attempts` (3) sends in total; after that it adopts what the car reports.
- **Logged only:** the firmware's status text (`charging`: Charging, Complete, Disconnected…) and `last_command`. Their wording may change between firmware releases, so decisions use the structured values.

### Fail-safe

- **No trustworthy reading → charge less, then stop.** The controller never keeps commanding power blind.
- **A failed or unconfirmed command is logged and re-sent; the loop never stops reading power.** A command gives up after `car_command_timeout_s` (10 s), and the ESP32 connection is kept alive in the background.
- **A stop from the Tesla app is permanent** until you start charging again (app or schedule).

---

## Repository layout

```text
volture/
├── esp32.yaml            # ESP32 firmware config (pulls yoziru/esphome-tesla-ble, pinned release)
├── secrets.example.yaml  # Template for secrets.yaml (gitignored), shared by firmware and Python
├── volture/           # What runs for real
│   ├── controller.py     # Pure control logic + its settings (Config)
│   ├── confirm.py        # Pure logic: did the car obey the last command?
│   ├── car.py            # EsphomeCar: the real car through the ESP32
│   ├── main.py           # Loop: read Shelly -> confirm -> decide -> command car
│   ├── pair.py           # One-off: pair the ESP32's key with the car
│   └── secrets.py        # Reads secrets.yaml (secrets + the volture: settings block)
├── simulator/            # Testing without hardware, never needed for real
│   ├── model.py          # Model 3 + house model
│   ├── server.py         # Fake Shelly Gen1 + fake car + control page (http://localhost:8081/)
│   └── car.py            # SimHttpCar: the simulated car, same interface as EsphomeCar
├── esp32/controller.h    # C++ port of controller.py + confirm.py + main.py's cycle (Loop), standard library only
├── esp32/device.h        # ESP32 adapters: read the car's entities, parse the Shelly reply, send commands
├── tests/
│   ├── test_controller.py  # Every scenario, run against Python and against the C++ port
│   ├── test_loop.py        # The C++ control cycle driving the simulated car, as on the ESP32
│   ├── conftest.py         # The two backends; compiles the C++ test driver into .tmp/
│   └── cpp/controller_cli.cpp  # Test driver: controller.h behind a stdin/stdout line protocol
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
git clone https://github.com/jaimeagudo/volture.git ~/github/volture
cd ~/github/volture
uv sync
uv run pytest -v
```

Every scenario runs twice: `[python]` and `[cpp]`. All must pass. Python 3.12–3.14 works. The `[cpp]` runs need a C++17 compiler (`xcode-select --install` on macOS); without one they are skipped.

**Changing the algorithm:** change `volture/controller.py` (or `confirm.py`) **and** `esp32/controller.h` the same way. If they differ, the `[cpp]` or `[python]` variant of some test fails.

### 3. Try the simulator (no hardware)

Terminal 1, the fake Shelly and car:
```bash
uv run python -m simulator.server
```

Terminal 2, the controller with the simulated Shelly and car:
```bash
uv run python -m volture.main --sim-shelly --sim-car
```

Nothing happens until you start charging: on the page, **Start charging** (the Tesla app's button). Volture then raises the amps 1 A every 15 s up to what fits.

Each piece is simulated independently; no flag means real hardware:

| Command                                        | Shelly                        | Car                     |
| ---------------------------------------------- | ----------------------------- | ----------------------- |
| `volture.main`                                 | real (`shelly_url`)           | real (via the ESP32)    |
| `volture.main --sim-shelly`                    | simulator (you type the power) | real                   |
| `volture.main --sim-car`                       | real                          | simulator               |
| `volture.main --sim-shelly --sim-car`          | simulator                     | simulator               |

The simulator is at `http://localhost:8081` by default; `--sim-url` points elsewhere (e.g. `simulator.server --port 8091` + `--sim-url http://localhost:8091`).

Open **http://localhost:8081/** (from a phone: `http://<your-Mac's-IP>:8081/`, Mac IP: `ipconfig getifaddr en0`). Two sections:
- **Shelly (house meter):** house power, presets, voltage, `is_valid`, `online`, and the exact `/emeter/0` JSON volture reads.
- **Simulated car:** its state; start or stop charging as if from the Tesla app (volture must follow it); *plugged in*; battery % and charge limit % (the car refuses or stops charging at its limit); untick *reachable* to make the car silently ignore volture's commands and watch the re-sends in the controller log.

The same controls with `curl`:

```bash
curl -X POST "localhost:8081/_sim/base?w=3500"          # house power 3500 W
curl -X POST "localhost:8081/_sim/shelly?voltage=215"   # measured voltage
curl -X POST "localhost:8081/_sim/shelly?valid=0"       # Shelly reports invalid readings
curl -X POST "localhost:8081/_sim/shelly?online=0"      # Shelly stops answering (HTTP 503)
curl -X POST "localhost:8081/_sim/car_link?reachable=0" # car ignores volture
curl -X POST "localhost:8081/_sim/car_link?plugged=0"   # cable unplugged
curl -X POST "localhost:8081/_sim/car_link?battery=80&limit=80"  # battery at its charge limit
curl -X POST "localhost:8081/_sim/car?force=1" -d '{"charging": true, "amps": 32}'  # Tesla app starts 32 A
```

Stop any program with `Ctrl + C`.

#### Be the Shelly while driving the real car

Before the Shelly is installed, you can debug with the **real ESP32 and car**: run `simulator.server`, then `volture.main --sim-shelly`, plug in and start charging from the Tesla app. Type only the **house** power on the page: Volture copies the real car's state into the page's simulated car, so the fake reading is house + real car (amps × V), as a real clamp would measure. The page's car section then shows the real car; do not use its buttons in this mode.

### 4. Fill in the secrets

```bash
cp secrets.example.yaml secrets.yaml && open -e secrets.yaml
```

| Key                            | What it is                                                                                                                                                                                                                                                                               |
| ------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `wifi_ssid`, `wifi_password`   | Your Wi-Fi. It must offer **2.4 GHz**; the ESP32 cannot use 5 GHz.                                                                                                                                                                                                                       |
| `wifi2_ssid`, `wifi2_password` | A second Wi-Fi (e.g. office and garage). The ESP32 joins whichever is in range. Both are required.                                                                                                                                                                                       |
| `web_password`                 | Password of the ESP32's web page (user `volture`). Any long random string.                                                                                                                                                                                                              |
| `wifi_hotspot_password`        | 8+ characters. The ESP32 opens its own Wi-Fi with it if it cannot join yours.                                                                                                                                                                                                            |
| `api_encryption_key`           | Encrypts Mac ↔ ESP32 traffic. Generate it with the command in the template.                                                                                                                                                                                                              |
| `tesla_vin`                    | The car's VIN (17 characters, in the Tesla app).                                                                                                                                                                                                                                         |
| `ble_mac_address`              | The car's Bluetooth **MAC address**, `AA:BB:CC:DD:EE:FF` (digits 0–9 and letters A–F only, so `0` is zero, never the letter `O`). See step 6.                                                                                                                                            |
| `shelly_url`                   | The real Shelly: `http://SHELLY_IP`. Ignored with `--sim-shelly`.                                                                                                                                                                                                                         |
| `volture:` block               | Every setting: `esphome_host`, the power limits, amps, steps and timings. Each line is commented in the template; a missing line keeps its default; a typo or impossible value stops volture at startup with a clear message. |

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

Later updates go over Wi-Fi: `uv run esphome run esp32.yaml --device volture.local`.

### 6. Find the car's Bluetooth MAC address

The car constantly advertises a Bluetooth name derived from the VIN: `S` + 16 hex characters + `C`. You need the **MAC address** of that advertisement, not the name.

- **Android (easiest):** install **nRF Connect**, scan next to the car, find the 18-character `S…C` name, copy the MAC shown under it.
- **No Android** (iPhones hide MAC addresses): in `esp32.yaml`, uncomment the `tesla_ble_listener` block, flash, take the ESP32 next to the car and watch the log for `Found Tesla vehicle | Name: … | MAC: …`. Put the MAC in `secrets.yaml`, comment the block again and flash again.

### 7. Pair the ESP32 with the car (in the car)

1. Sit in the car with the ESP32 powered and within Bluetooth range, and the Mac on the same Wi-Fi.
2. Run:
   ```bash
   uv run python -m volture.pair
   ```
3. **Immediately** tap the NFC key card on the centre console and confirm the prompt on the car's screen.
4. Check *Controls → Locks*: a new key ("Unknown device") appears. Rename it to "volture" if you like.

The key is paired with the **`CHARGING_MANAGER`** role (set in `esp32.yaml`): it can start/stop charging and set amps, but cannot unlock or drive. Changing the role requires pairing again.

### 8. Install the Shelly EM

1. Power the Shelly, join its Wi-Fi `shellyem-XXXXXXXXXXXX`, open `http://192.168.33.1/` and enter your Wi-Fi.
2. Reserve a **fixed IP** for it in your router; update its firmware.
3. Put the clamp around **one single conductor** of the main feed (not phase and neutral together: they cancel out). **If you are not comfortable working in the electrical panel, have an electrician do it.**
4. Check: `curl http://SHELLY_IP/emeter/0` returns `power`, `voltage`, `is_valid`. Switch on a known load (a heater): `power` must rise by that amount and be **positive**. Negative means the clamp is backwards.

### 9. Run for real

In `secrets.yaml` set `shelly_url: "http://SHELLY_IP"`, then:

```bash
uv run python -m volture.main
```

Everything comes from `secrets.yaml`. Then start charging as usual (Tesla app or the car's schedule); Volture takes it from there.

### Watching the ESP32

Open **http://volture.local** (phone or Mac, on your Wi-Fi), user `volture`, password `web_password` from `secrets.yaml`. The page shows every value live and lets you change two things:

| Entity                         | Meaning                                                                                                     |
| ------------------------------ | ----------------------------------------------------------------------------------------------------------- |
| **Volture control** (switch)   | Off (default): observe only, nothing sent to the car. On: the ESP32 drives the car. Kept across reboots. Never on together with `volture.main` on the Mac |
| **Shelly URL** (text)          | Where the ESP32 reads power. The real Shelly, or the Mac's fake one: `http://<Mac IP>:8081` (not `localhost`) |
| House power, Grid voltage      | The last Shelly reading (empty: no reading)                                                                 |
| Volture amps                   | Amps the loop has decided (0: stopped)                                                                      |
| Volture decision               | Why, e.g. `ramp`, `no_room`, `amps_cap`, `over_limit_drop`, `no_reading_stop`, `idle`; plus what it sent (or *would send* when control is off) |
| Volture event                  | What the car did with the last command: `confirmed`, `waiting`, `resend`, `outside_change`, `dry_run`…    |
| Heap free / largest block / min free | ESP32 memory, to spot leaks                                                                           |

For tools, the same data is plain JSON: `GET http://volture.local/events` streams one JSON line per change (Server-Sent Events); `GET /sensor/House%20power` returns one value. Every cycle the log also prints one line in InfluxDB line-protocol form (`volture power_w=4330,amps=16,reason="no_room",…`): `uv run esphome logs esp32.yaml --device volture.local`.

### Troubleshooting

| Symptom                                                                                           | Likely cause                                                                                                                                                                     |
| ------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| No `/dev/cu.usbserial*`                                                                           | Charge-only cable, bad port, or missing CP210x driver                                                                                                                            |
| ESP32 log: `wifi … Auth Expired` once, then `Connected`                                           | Normal on some routers: it retries                                                                                                                                               |
| ESP32 never connects to Wi-Fi                                                                     | Wrong password, or the network is 5 GHz only                                                                                                                                     |
| `Invalid encryption key`                                                                          | `api_encryption_key` differs from the one the ESP32 was flashed with: flash again                                                                                                |
| `Timeout while resolving IP address for volture.local`                                            | The Mac has not discovered the new name yet; retry, or set `esphome_host` to the ESP32's IP                                                                                      |
| `ESP32 not connected` / `car state unavailable` all the time, though `ping volture.local` answers | Weak ESP32 Wi-Fi: small packets pass, large ones are lost (check `ping -s 1400 volture.local`). Move the ESP32 or the router closer; aim for a `WiFi Signal` above about -75 dBm |
| ESP32 log: `Command failed: … Connection lost`                                                    | Car out of Bluetooth range, asleep, or not paired yet                                                                                                                            |
| Pairing: no prompt on the car                                                                     | Wrong MAC/VIN in `secrets.yaml`, or the NFC card was not tapped right away                                                                                                       |
| Upload ends with `Finishing update failed`                                                        | Firmware built for another board; this board needs `tesla-ble-esp32-generic`                                                                                                     |
| Controller drops to 5 A then stops                                                                | Shelly unreachable or `is_valid: false` (intended fail-safe)                                                                                                                     |

---

## Decisions

1. **Shelly EM Gen1 (€37) over Gen3 (€60).** Power + `is_valid` over a plain HTTP GET is all that's needed; on-device history is irrelevant because decisions happen every few seconds. Accepted risk: Gen1 firmware is frozen at 1.14.0 (2023) and only gets fixes.
2. **The Shelly cannot replace the ESP32.** Shelly scripts can only scan BLE adverts (no GATT connection) and have ~25 KB of memory; Tesla's BLE protocol needs elliptic-curve crypto and protobuf.
3. **Everything runs on the ESP32, 24/7, no other machine and no Home Assistant** (decided 2026-10-15). The control algorithm is ported to C++ (`esp32/controller.h`, tested) and runs on the ESP32, observe-only until "Volture control" is switched on (decision 15); until then the Python loop on a Mac drives the car. The Python version stays for debugging on a Mac, and both implementations must pass the same test scenarios so they cannot drift.
4. **ESPHome native API for the Mac; a password-protected web page for people.** The native API (Mac ↔ ESP32) is encrypted with a key. The web page (decision 16) uses a password, but over plain HTTP: someone on the same Wi-Fi who captures traffic could read the password, and the page can also operate the car's charging controls. Accepted for a home LAN; do not expose `volture.local` to the internet.
5. **Firmware from `esp32.yaml`, not a local clone.** `esp32.yaml` pulls the yoziru firmware from GitHub at a pinned release. No clone, no Makefile; upgrading is changing `tesla_ble_ref` (and the matching `esphome` version in `pyproject.toml`).
6. **Least-privileged car key** (`CHARGING_MANAGER`): a leaked key can only affect charging.
7. **One file to configure everything** (`secrets.yaml`): secrets shared by firmware and Python, plus every algorithm setting in its `volture:` block.
8. **Board:** classic ESP32 → `tesla-ble-esp32-generic`. Alternatives (M5Stack NanoC6, Seeed XIAO ESP32-C6) only matter if Bluetooth range in the garage turns out poor (external antenna).
9. **Restart threshold:** restart only if *house + 5 A* fits under the ceiling (house < 3,220 W); restarting below 3,910 W, as first proposed, would exceed the contracted power at 5 A.
10. **Proportional drop:** one step of ⌈(P − 4,370) / V⌉ A corrects a big spike at once instead of 1 A per cycle.
11. **Confirm with the car's state, not `last_command` text:** the switch/amps states are structured; `last_command` is firmware prose that may change between releases.
12. **No time-based charging, ever** (decided 2026-10-15). Volture has no schedule, off-peak window or clock-based rule. The car itself or the owner (Tesla app) decide when to charge, and whether the cable is plugged in. A schedule in Volture would duplicate the car's own scheduling: a second source of truth that drifts. Volture only keeps the house under its contracted power and follows the Tesla app.
13. **Volture never starts a charge** (decided 2026-10-15). The owner starts it (Tesla app or the car's schedule); Volture then uses every amp that fits under the contracted power, whatever amps the app set. The only exception: a charge Volture itself stopped to protect the contracted power (or for lack of Shelly readings) is resumed once there is room again; otherwise one spike at night would end the whole night's charge. Which pieces are simulated is chosen per run with `--sim-shelly` / `--sim-car`, not in `secrets.yaml`.
14. **Hard amps cap from basic maths** (decided 2026-10-15). Amps never exceed the lower of `max_amps` and ⌊`contracted_power_w` / V⌋ (20 A at 4,600 W / 230 V), whatever the Shelly reports. Added after a test where a fake Shelly reading that did not include the car let Volture reach 22 A (5,060 W). Since then `--sim-shelly` with the real car adds the real car's power to the fake reading, as a real clamp would.
15. **The ESP32's loop starts observe-only** (decided 2026-10-15). Switch "Volture control" on its web page decides whether it drives the car; off by default, then kept across reboots. With it off the loop still reads everything and shows what it *would* send, so its decisions can be compared with the Mac's before handing over. Never run both in control at once.
16. **The ESP32 always serves its web page** (owner's requirement, 2026-10-15): live values and the JSON stream at `/events`, at `http://volture.local`. It is never removed or made optional to save memory or flash.
17. **No data pushed to the internet, for now** (decided 2026-10-15). The ESP32 only talks plain HTTP to the Shelly on the LAN. Pushing to a cloud service (Grafana) works over HTTPS with Bluetooth connected, but needs about 40 KB of memory at peak and fragments it; options and measurements in [issue #12](https://github.com/jaimeagudo/volture/issues/12).

---

## To do

- Install the Shelly (steps 8–9) and run a real charge with it. Check in the log that the car's reported state confirms each command (`car confirmed …`).
- Measure how often the Shelly Gen1 refreshes `power` (`curl` every second); it bounds how short `poll_s` can be.
- Confirm the clamp model (50 A or 120 A) covers the house's peak. The installation is single-phase.
- Hand control to the ESP32 for real ("Volture control" on, Mac loop off) once the real Shelly is in.
- Settings on the ESP32's page: [issue #13](https://github.com/jaimeagudo/volture/issues/13) (today the built-in defaults, identical to `secrets.example.yaml`).
- A slow car confirmation can be mistaken for a change made in the Tesla app: [issue #14](https://github.com/jaimeagudo/volture/issues/14).
- Remote storage / Grafana, deferred: [issue #12](https://github.com/jaimeagudo/volture/issues/12).

## Possible improvements

Solar-surplus mode · history and charts ([issue #12](https://github.com/jaimeagudo/volture/issues/12)) · three-phase support (Shelly 3EM) · failure notifications · external-antenna board if BLE range is poor.

---

## History

| Date       | PR | What changed |
| ---------- | -- | ------------ |
| 2026-10-14 | #1–#2 | Python controller + simulator; ESP32 firmware config (`esp32.yaml`, yoziru/esphome-tesla-ble, `CHARGING_MANAGER` key); real car control through the ESP32 |
| 2026-10-15 | #3 | Second Wi-Fi; `shelly_url` setting; fake Shelly page; 10 s car command timeout |
| 2026-10-15 | #4 | `simulator/` folder; every setting in `secrets.yaml`; measured voltage; commands confirmed with the car's reported state (also an off-peak window, removed in #6) |
| 2026-10-15 | #5 | Project renamed SirWattson → Volture; the Tesla app has priority; never start an unplugged or full car |
| 2026-10-15 | #6–#7 | Time-based charging removed for good (decision 12); C++ port `esp32/controller.h`, every scenario run against Python and C++ |
| 2026-10-15 | #8 | Volture never starts a charge, uses every amp that fits (decision 13); `--sim-shelly` / `--sim-car` flags replace `car:` |
| 2026-10-15 | #9 | Fixed `CoverState` errors and a hang at startup (aioesphomeapi 46.3.0 zeroconf listener bug, worked around) |
| 2026-10-15 | #10 | Hard 20 A cap (decision 14); fake Shelly includes the real car's load; no redundant "start" |
| 2026-10-15 | #11 | C++ loop running on the ESP32, observe-only by default; web page with live JSON (decisions 15–16); HTTPS memory measured |
| 2026-10-15 | this | HTTPS test removed: no internet traffic from the ESP32 (decision 17); issues #12–#14 opened |

## Links

- [yoziru/esphome-tesla-ble](https://github.com/yoziru/esphome-tesla-ble): the ESP32 firmware this project runs
- [Shelly Gen1 API](https://shelly-api-docs.shelly.cloud/gen1/) · [Shelly EM](https://kb.shelly.cloud/knowledge-base/shelly-em)
- [aioesphomeapi](https://github.com/esphome/aioesphomeapi): Python client for the ESPHome native API
- [ESPHome native API](https://esphome.io/components/api/) · [uv](https://docs.astral.sh/uv/)

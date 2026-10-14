# SirWattson

Keeps house + Tesla Model 3 charging under the contracted 4.6 kW. A Shelly EM (Gen1) clamp on the main feed
reports total power; the controller adjusts the car's amps (5-32 A) through an ESP32 running
[yoziru/esphome-tesla-ble](https://github.com/yoziru/esphome-tesla-ble).

## Run without hardware

    uv sync
    uv run python -m sirwattson.sim_server    # fake Shelly + fake car (port 8081)
    uv run python -m sirwattson.main          # controller
    curl -X POST 'localhost:8081/_sim/base?w=3500'   # simulate a load spike
    curl localhost:8081/_sim/state

Tests (stdlib only): `python3 -m unittest discover -s tests`

Secrets live in `secrets.yaml` (gitignored); see `secrets.example.yaml`.

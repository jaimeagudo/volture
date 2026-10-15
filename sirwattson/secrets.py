"""Reads secrets.yaml: the single file for secrets AND settings, shared with the ESP32 firmware.

Top-level keys are secrets (some used by esp32.yaml). The `volture:` block holds every setting
of the algorithm and the main loop; any key left out keeps its default (see Config, LoopSettings).
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path

import yaml

from .controller import Config

SECRETS_PATH = Path(__file__).resolve().parent.parent / "secrets.yaml"
SETTINGS_BLOCK = "volture"


CAR_KINDS = ("esphome", "sim")


@dataclass(frozen=True)
class LoopSettings:
    car: str = "esphome"                 # esphome: real car via the ESP32; sim: simulator.server's car
    esphome_host: str = "volture.local"
    poll_s: float = 5.0                  # how often the Shelly is read
    car_command_timeout_s: float = 10.0  # a command never stalls power readings longer than this
    confirm_timeout_s: float = 15.0      # wait this long for the car to report the commanded state
    confirm_max_attempts: int = 3        # sends per command before trusting the car's state
    timezone: str = "Europe/Madrid"      # for the off-peak window hours


def _load(path: Path) -> dict:
    try:
        return yaml.safe_load(path.read_text()) or {}
    except FileNotFoundError:
        return {}
    except (OSError, yaml.YAMLError) as e:
        raise SystemExit(f"cannot read {path}: {e}") from e


def read_secret(name: str, default: str | None = None, path: Path = SECRETS_PATH) -> str:
    """Value of `name` in secrets.yaml; `default` when the file or key is missing (if given)."""
    value = _load(path).get(name)
    if value:
        return str(value)
    if default is not None:
        return default
    raise SystemExit(f"{name} missing in {path} (template: secrets.example.yaml)")


def read_settings(path: Path = SECRETS_PATH) -> tuple[Config, LoopSettings]:
    block = _load(path).get(SETTINGS_BLOCK) or {}
    if not isinstance(block, dict):
        raise SystemExit(f"'{SETTINGS_BLOCK}:' in {path} must be a block of key: value lines")
    config_keys = {f.name for f in fields(Config)}
    loop_keys = {f.name for f in fields(LoopSettings)}
    unknown = set(block) - config_keys - loop_keys
    if unknown:
        raise SystemExit(f"unknown settings {sorted(unknown)} in {path}; "
                         f"valid: {sorted(config_keys | loop_keys)}")
    try:
        cfg = Config(**{k: v for k, v in block.items() if k in config_keys})
        loop = LoopSettings(**{k: v for k, v in block.items() if k in loop_keys})
    except TypeError as e:
        raise SystemExit(f"bad settings in {path}: {e}") from e
    _check(cfg, loop)
    return cfg, loop


def _check(cfg: Config, loop: LoopSettings) -> None:
    problems = []
    if loop.car not in CAR_KINDS:
        problems.append(f"car must be one of {CAR_KINDS}")
    if not 0 < cfg.min_amps <= cfg.max_amps:
        problems.append("need 0 < min_amps <= max_amps")
    if cfg.working_limit_w <= 0:
        problems.append("need headroom_w < contracted_power_w")
    for name in ("offpeak_start_hour", "offpeak_end_hour"):
        hour = getattr(cfg, name)
        if hour is not None and not (isinstance(hour, int) and 0 <= hour <= 23):
            problems.append(f"{name} must be a whole hour 0-23")
    if (cfg.offpeak_start_hour is None) != (cfg.offpeak_end_hour is None):
        problems.append("set both offpeak_start_hour and offpeak_end_hour, or neither")
    if problems:
        raise SystemExit("bad settings: " + "; ".join(problems))

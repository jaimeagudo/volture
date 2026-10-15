"""Reads secrets.yaml, the single secrets file shared with the ESP32 firmware (esp32.yaml)."""
from __future__ import annotations

from pathlib import Path

import yaml

SECRETS_PATH = Path(__file__).resolve().parent.parent / "secrets.yaml"


def read_secret(name: str, default: str | None = None, path: Path = SECRETS_PATH) -> str:
    """Value of `name` in secrets.yaml; `default` when the file or key is missing (if given)."""
    try:
        value = (yaml.safe_load(path.read_text()) or {}).get(name)
    except FileNotFoundError:
        value = None
    except (OSError, yaml.YAMLError) as e:
        raise SystemExit(f"cannot read {path}: {e}") from e
    if value:
        return str(value)
    if default is not None:
        return default
    raise SystemExit(f"{name} missing in {path} (template: secrets.example.yaml)")

"""Real car adapter: drives the Tesla through the ESP32 over the ESPHome native API.

aioesphomeapi's ReconnectLogic keeps one encrypted connection alive in the background, so
neither reading the car's state nor sending a command ever waits on a (re)connect:
`observe()` returns the last reported state, `apply()` raises at once when offline.
The ESP32 only forwards commands over BLE; they reach the car only if paired and in range.
"""
from __future__ import annotations

import logging
from typing import Optional

from aioesphomeapi import APIClient, ReconnectLogic

from .confirm import CarState
from .controller import Command

# object_id of the entities exposed by the yoziru/esphome-tesla-ble firmware (listed from the flashed ESP32).
AMPS_ENTITY = "charging_amps"       # number: charging current setpoint (A), as reported by the car
CHARGER_ENTITY = "charger"          # switch: charging on/off, as reported by the car
LAST_COMMAND_ENTITY = "last_command"  # text: firmware's outcome of the last BLE command (logged only)
ESPHOME_API_PORT = 6053

log = logging.getLogger("volture.car")


class EsphomeCar:
    def __init__(self, host: str, noise_psk: str):
        self.host, self.noise_psk = host, noise_psk
        self.client: Optional[APIClient] = None  # created in start(): it needs a running event loop
        self.connected = False
        self.keys: dict[str, int] = {}
        self.states: dict[int, object] = {}  # entity key -> last reported value (missing = unknown)
        self.reconnect: Optional[ReconnectLogic] = None

    async def start(self) -> None:
        self.client = APIClient(self.host, ESPHOME_API_PORT, None, noise_psk=self.noise_psk)
        self.reconnect = ReconnectLogic(client=self.client, on_connect=self._on_connect,
                                        on_disconnect=self._on_disconnect, name="volture-esp32")
        await self.reconnect.start()

    async def stop(self) -> None:
        if self.reconnect is not None:
            await self.reconnect.stop()
        if self.client is None:
            return
        try:
            await self.client.disconnect(force=True)
        except Exception:  # already closed
            pass

    async def _on_connect(self) -> None:
        try:
            entities, _services = await self.client.list_entities_services()
            self.keys = {entity.object_id: entity.key for entity in entities}
            missing = {AMPS_ENTITY, CHARGER_ENTITY} - self.keys.keys()
            if missing:
                log.error("ESP32 firmware lacks entities %s", sorted(missing))
                return
            self.client.subscribe_states(self._on_state)
            self.connected = True
            log.info("ESP32 connected")
        except Exception as e:
            # e.g. weak Wi-Fi: handshake passes, entity list is lost. ReconnectLogic retries.
            log.warning("ESP32 setup failed: %r", e)
            await self.client.disconnect(force=True)

    async def _on_disconnect(self, expected: bool) -> None:
        self.connected = False
        self.states.clear()
        log.warning("ESP32 disconnected")

    def _on_state(self, state) -> None:
        if getattr(state, "missing_state", False):
            self.states.pop(state.key, None)
            return
        if state.key == self.keys.get(LAST_COMMAND_ENTITY) and self.states.get(state.key) != state.state:
            log.info("car last_command: %s", state.state)
        self.states[state.key] = state.state

    async def observe(self) -> Optional[CarState]:
        charging = self.states.get(self.keys.get(CHARGER_ENTITY))
        amps = self.states.get(self.keys.get(AMPS_ENTITY))
        if not self.connected or charging is None or amps is None:
            return None
        return CarState(bool(charging), int(round(float(amps))))

    async def apply(self, cmd: Command) -> None:
        if not self.connected:
            raise ConnectionError("ESP32 not connected")
        if cmd.charging:
            # Amps first, so charging never (re)starts at a stale, higher setpoint.
            self.client.number_command(self.keys[AMPS_ENTITY], float(cmd.amps))
        self.client.switch_command(self.keys[CHARGER_ENTITY], cmd.charging)
